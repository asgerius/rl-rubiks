from collections import deque
from typing import List

import numpy as np
import torch

from src.rubiks import gpu, no_grad
from src.rubiks.model import Model
from src.rubiks.cube.cube import Cube
from src.rubiks.utils import seedsetter
from src.rubiks.utils.logger import Logger
from src.rubiks.utils.ticktock import TickTock

class Node:
	def __init__(self, state: np.ndarray, policy: np.ndarray, value: float, from_node=None, action_idx: int=None):
		self.is_leaf = True  # When initiated, the node is leaf of search graph
		self.state = state
		self.P = policy
		self.value = value
		# self.neighs[i] is a tuple containing the state obtained by the action Cube.action_space[i]
		# strings are used, so they can be used for lookups
		self.neighs = [None] * Cube.action_dim
		self.N = np.zeros(Cube.action_dim)
		self.W = np.zeros(Cube.action_dim)
		self.L = np.zeros(Cube.action_dim)
		if action_idx is not None:
			from_action_idx = Cube.rev_action(action_idx)
			self.neighs[from_action_idx] = from_node

	def __str__(self):
		return "\n".join([
			"----- Node -----",
			f"Leaf:      {self.is_leaf}",
			f"State:     {tuple(self.state)}",
			f"Value:     {self.value}",
			f"N:         {self.N}",
			f"W:         {self.W}",
			f"Neighbors: {[id(x) if x is not None else None for x in self.neighs]}",
			"----------------",
		])


class Searcher:
	eps = np.finfo("float").eps
	_explored_states = 0

	def __init__(self):
		self.action_queue = deque()
		self.tt = TickTock()

	@no_grad
	def search(self, state: np.ndarray, time_limit: float) -> bool:
		# Returns whether a path was found and generates action queue
		# Implement _step method for searchers that look one step ahead, otherwise overwrite this method
		self.reset()
		self.tt.tick()
		if Cube.is_solved(state): return True
		while self.tt.tock() < time_limit:
			action, state, solution_found = self._step(state)
			self.action_queue.append(action)
			if solution_found:
				self._explored_states = len(self.action_queue)
				return True

		self._explored_states = len(self.action_queue)
		return False

	def _step(self, state: np.ndarray) -> (int, np.ndarray, bool):
		raise NotImplementedError

	def reset(self):
		self._explored_states = 0
		self.action_queue = deque()
		self.tt.reset()
		if hasattr(self, "net"):
			self.net.eval()

	def __str__(self):
		raise NotImplementedError

	def __len__(self):
		# Returns number of states explored
		# FIXME: Will not work with external multithreading
		return self._explored_states


class DeepSearcher(Searcher):
	def __init__(self, net: Model):
		super().__init__()
		self.net = net

	@classmethod
	def from_saved(cls, loc: str):
		net = Model.load(loc)
		net.to(gpu)
		return cls(net)

	def _step(self, state: np.ndarray) -> (int, np.ndarray, bool):
		raise NotImplementedError


class RandomDFS(Searcher):
	def _step(self, state: np.ndarray) -> (int, np.ndarray, bool):
		action = np.random.randint(Cube.action_dim)
		state = Cube.rotate(state, *Cube.action_space[action])
		return action, state, Cube.is_solved(state)

	def __str__(self):
		return "Random depth-first search"

class BFS(Searcher):
	def search(self, state: np.ndarray, time_limit: float) -> (np.ndarray, bool):
		self.reset()
		self.tt.tick()

		if Cube.is_solved(state): return True

		# TODO: Speed this up using multi_rotate
		# Each element contains the state from which it came and the corresponding action
		states = { state.tostring(): (None, None) }
		queue = deque([state])
		while self.tt.tock() < time_limit:
			state = queue.popleft()
			tstate = state.tostring()
			for i, action in enumerate(Cube.action_space):
				new_state = Cube.rotate(state, *action)
				new_tstate = new_state.tostring()
				if new_tstate in states:
					continue
				elif Cube.is_solved(new_state):
					self.action_queue.appendleft(i)
					while states[tstate][0] is not None:
						self.action_queue.appendleft(states[tstate][1])
						tstate = states[tstate][0]
					self.explored_states = len(self.action_queue)
					return True
				else:
					states[new_tstate] = (tstate, i)
					queue.append(new_state)

		self.explored_states = len(self.action_queue)
		return False

	def __str__(self):
		return "Breadth-first search"


class PolicySearch(DeepSearcher):

	def __init__(self, net: Model, sample_policy=False):
		super().__init__(net)
		self.sample_policy = sample_policy

	def _step(self, state: np.ndarray) -> (int, np.ndarray, bool):
		policy = torch.nn.functional.softmax(self.net(Cube.as_oh(state), value=False).cpu(), dim=1).numpy().squeeze()
		action = np.random.choice(Cube.action_dim, p=policy) if self.sample_policy else policy.argmax()
		state = Cube.rotate(state, *Cube.action_space[action])
		return action, state, Cube.is_solved(state)

	@classmethod
	def from_saved(cls, loc: str, sample_policy=False):
		net = Model.load(loc)
		net.to(gpu)
		return cls(net, sample_policy)

	def __str__(self):
		return f"Policy search {'with' if self.sample_policy else 'without'} sampling"


class MCTS(DeepSearcher):

	_expand_nodes = 1000  # Expands stack by 1000, then 2000, then 4000 and etc. each expansion
	n_states = 0
	indices = dict()  # Key is state.tostring(). Contains index of state in the next arrays. Index 0 is not used
	states: np.ndarray
	neighbors: np.ndarray  # n x 12 array of neighbor indices. As first index is unused, np.all(self.neighbors, axis=1) can be used
	leaves: np.ndarray  # Boolean vector containing whether a node is a leaf
	P: np.ndarray
	V: np.ndarray
	N: np.ndarray
	W: np.ndarray
	L: np.ndarray

	def __init__(self, net: Model, c: float, nu: float, complete_graph: bool, search_graph: bool, workers: int):
		super().__init__(net)
		self.c = c
		self.nu = nu
		self.complete_graph = complete_graph
		self.search_graph = search_graph
		self.workers = workers

		self.expand_nodes = 1000

	def reset(self):
		super().reset()
		self.indices   = dict()
		self.states    = np.empty((self.expand_nodes, *Cube.shape()), dtype=Cube.dtype)
		self.neighbors = np.zeros((self.expand_nodes, Cube.action_dim), dtype=int)
		self.leaves    = np.ones(self.expand_nodes, dtype=bool)
		self.P         = np.empty((self.expand_nodes, Cube.action_dim))
		self.V         = np.empty(self.expand_nodes)
		self.N         = np.zeros((self.expand_nodes, Cube.action_dim), dtype=int)
		self.W         = np.zeros((self.expand_nodes, Cube.action_dim))
		self.L         = np.zeros((self.expand_nodes, Cube.action_dim))

	def increase_stack_size(self):
		expand_size    = len(self.states)
		self.states	   = np.concatenate([self.states, np.empty((expand_size, *Cube.shape()), dtype=Cube.dtype)])
		self.neighbors = np.concatenate([self.neighbors, np.zeros((expand_size, Cube.action_dim), dtype=int)])
		self.leaves    = np.concatenate([self.leaves, np.ones(expand_size, dtype=bool)])
		self.P         = np.concatenate([self.P, np.empty((expand_size, Cube.action_dim))])
		self.V         = np.concatenate([self.V, np.empty(expand_size)])
		self.N         = np.concatenate([self.N, np.zeros((expand_size, Cube.action_dim), dtype=int)])
		self.W         = np.concatenate([self.W, np.zeros((expand_size, Cube.action_dim))])
		self.L         = np.concatenate([self.L, np.zeros((expand_size, Cube.action_dim))])

	@no_grad
	def search(self, state: np.ndarray, time_limit: float, max_states: int) -> bool:
		self.reset()
		self.tt.tick()

		self.indices[state.tostring()] = 1
		self.states[1] = state
		if Cube.is_solved(state): return True
		oh = Cube.as_oh(state)
		p, v = self.net(oh)
		self.P[1] = p.softmax(dim=1).cpu().numpy()
		self.V[1] = v.cpu().numpy()

		paths = [deque()]
		leaves = np.array([1], dtype=int)
		while self.tt.tock() < time_limit and len(self) < max_states:
			self.tt.profile("Expanding leaves")
			solve_leaf, solve_action = self.expand_leaves(leaves)
			self.tt.end_profile("Expanding leaves")

			# If a solution is found
			if solve_leaf != -1:
				self.action_queue = paths[solve_leaf] + deque([solve_action])
				if self.search_graph:
					self._shorten_action_queue()
				return True

			# Find leaves
			paths, leaves = zip(*[self.find_leaf(time_limit) for _ in range(self.workers)])

		return False

	def expand_leaves(self, leaves_idcs: np.ndarray) -> (int, int):
		"""
		Expands all given states which are given by the indices in leaves_idcs
		Returns the index of the leaf and the action to solve it
		Both are -1 if no solution is found
		"""
		# print("EXPANDING LEAVES")
		# print(self.states[leaves_idcs])
		leaf_idx, action_idx = -1, -1
		# Ensure space in stacks
		if len(self.indices) + len(leaves_idcs) * Cube.action_dim + 1 > len(self.states):
			self.tt.profile("Increase stack size")
			self.increase_stack_size()
			self.tt.end_profile("Increase stack size")
		
		# Explore new states
		self.tt.profile("Getting new states")
		states = self.states[leaves_idcs]
		substates = Cube.multi_rotate(np.repeat(states, Cube.action_dim, axis=0), *Cube.iter_actions(len(states)))
		self.tt.end_profile("Getting new states")

		actions_taken = np.tile(np.arange(Cube.action_dim), len(leaves_idcs))
		repeated_leaves_idcs = np.repeat(leaves_idcs, Cube.action_dim)

		# Check for solution and return if found
		self.tt.profile("Checking for solved state")  # TODO: Consider moving this to later
		solved_new_states = Cube.multi_is_solved(substates)
		solved_new_states_idcs = np.where(solved_new_states)[0]
		if solved_new_states_idcs.size:
			i = solved_new_states_idcs[0]
			leaf_idx, action_idx = i // Cube.action_dim, actions_taken[i]
			self._update_neighbors(leaves_idcs[leaf_idx])
		self.tt.end_profile("Checking for solved state")

		self.tt.profile("Substate strings")
		substate_strs		= [s.tostring() for s in substates]
		get_substate_strs	= lambda bools: [s for s, b in zip(substate_strs, bools) if b]  # Alternative to boolean list indexing
		self.tt.end_profile("Substate strings")

		self.tt.profile("New/old substates")
		seen_substates		= np.array([s in self.indices for s in substate_strs])  # Boolean array: Substates that have been seen before
		unseen_substates	= ~seen_substates  # Boolean array: Substates that have not been seen before
		self.tt.end_profile("New/old substates")

		self.tt.profile("Handle duplicates")
		last_occurences		= np.array([s not in substate_strs[i+1:] for i, s in enumerate(substate_strs)])  # To prevent duplicates. O(n**2) goes brrrr
		last_seen			= last_occurences & seen_substates  # Boolean array: Last occurances of substates that have been seen before
		last_unseen			= last_occurences & unseen_substates  # Boolean array: Last occurances of substates that have not been seen before
		self.tt.end_profile("Handle duplicates")

		self.tt.profile("Update indices of new states")
		new_states			= substates[last_unseen]  # Substates that are not already in the graph. Without duplicates
		new_states_idcs		= len(self.indices) + np.arange(last_unseen.sum()) + 1  # Indices in self.states corresponding to new_states
		new_idcs_dict		= { s: i for i, s in zip(new_states_idcs, get_substate_strs(last_unseen)) }
		self.indices.update(new_idcs_dict)
		substate_idcs		= np.array([self.indices[s] for s in substate_strs])
		self.tt.end_profile("Update indices of new states")

		old_states			= substates[last_seen]  # Substates that are already in the graph. Without duplicates
		old_states_idcs		= substate_idcs[last_seen]  # Indices in self.states corresponding to old_states

		# Update states and neighbors
		self.tt.profile("Update neighbors")
		self.states[new_states_idcs] = substates[last_unseen]
		self.neighbors[repeated_leaves_idcs, actions_taken] = substate_idcs
		self.neighbors[substate_idcs, Cube.rev_actions(actions_taken)] = repeated_leaves_idcs
		self.tt.end_profile("Update neighbors")

		self.tt.profile("One-hot encoding")
		new_states_oh = Cube.as_oh(new_states)
		self.tt.end_profile("One-hot encoding")
		self.tt.profile("Feedforward")
		p, v = self.net(new_states_oh)
		p, v = p.softmax(dim=1).cpu().numpy(), v.squeeze().cpu().numpy()  # TODO: Possible bug where v will by squeezed too much if it is 1x1
		self.tt.end_profile("Feedforward")

		self.tt.profile("Update p and v")
		# Updates all values for new states
		self.P[new_states_idcs] = p
		self.V[new_states_idcs] = v
		self.tt.end_profile("Update p and v")

		# Updates leaves
		self.tt.profile("Update leaves")
		self.leaves[leaves_idcs] = False
		self.leaves[old_states_idcs[self.neighbors[old_states_idcs].all(axis=1)]] = False
		self.tt.end_profile("Update leaves")

		self.tt.profile("Update W")
		neighbor_idcs = self.neighbors[leaves_idcs]
		for neighs in neighbor_idcs:  # TODO: Drop this loop
			W = self.V[neighs].max()
			self.W[neighs, Cube.rev_actions(np.arange(Cube.action_dim))] = W
		self.tt.end_profile("Update W")

		return leaf_idx, action_idx

	def _update_neighbors(self, state_idx: int):
		"""
		Expands around state. If a new state is already in the tree, neighbor relations are updated
		Assumes that state is already in the tree
		Used for node expansion
		TODO: Consider not using state_idx and instead all leaves
		"""
		self.tt.profile("Update neighbors")
		state = self.states[state_idx]
		substates = Cube.multi_rotate(np.repeat([state], Cube.action_dim, axis=0), *Cube.iter_actions())
		actions_taken = np.array([i for i, s in enumerate(substates) if s.tostring() in self.indices], dtype=int)
		substate_idcs = np.array([self.indices[s.tostring()] for s in substates[actions_taken]])
		self.neighbors[[state_idx]*len(actions_taken), actions_taken] = substate_idcs
		self.neighbors[substate_idcs, Cube.rev_actions(actions_taken)] = state_idx
		self.leaves[state_idx] = False
		self.leaves[[state_idx, *substate_idcs]] = self.neighbors[[state_idx, *substate_idcs]].all(axis=1)
		self.tt.end_profile("Update neighbors")

	def find_leaf(self, time_limit: float) -> (list, np.ndarray):
		"""
		Searches the tree starting from starting state using self.workers workers
		Returns a list of paths and an array containing indices of leaves
		"""
		path = deque()
		current_index = 1
		self.tt.profile("Exploring next node")
		while not self.leaves[current_index] and self.tt.tock() < time_limit:
			self.tt.profile("sqrt(N)")
			sqrtN = np.sqrt(self.N[current_index].sum())
			self.tt.end_profile("sqrt(N)")
			if sqrtN < self.eps:
				# If no actions have been taken from this before
				self.tt.profile("Random actions")
				action = np.random.randint(Cube.action_dim)
				self.tt.end_profile("Random actions")
			else:
				# If actions have been taken from this state before
				self.tt.profile("U")
				U = self.c * self.P[current_index] * sqrtN / (1 + self.N[current_index])
				self.tt.end_profile("U")
				self.tt.profile("Q")
				Q = self.W[current_index] - self.L[current_index]
				self.tt.end_profile("Q")
				self.tt.profile("Actions")
				action = (U + Q).argmax()
				self.tt.end_profile("Actions")
			# Updates N and virtual loss
			self.tt.profile("Update N and L")
			# TODO: Perhaps use the fast buggy code here
			self.N[current_index, action] += 1
			self.L[current_index, action] += self.nu
			self.tt.end_profile("Update N and L")
			self.tt.profile("Update paths")
			path.append(action)
			self.tt.end_profile("Update paths")
			self.tt.profile("Get next state")
			current_index = self.neighbors[current_index, action]
			self.tt.end_profile("Get next state")
		self.tt.end_profile("Exploring next node")
		# print(paths)
		return path, current_index

	def _shorten_action_queue(self):
		# TODO
		pass

	@classmethod
	def from_saved(cls, loc: str, c: float, nu: float, complete_graph: bool, search_graph: bool, workers: int):
		net = Model.load(loc)
		net.to(gpu)
		return cls(net, c=c, nu=nu, complete_graph=complete_graph, search_graph=search_graph, workers=workers)

	def __str__(self):
		return f"MCTS {'with' if self.search_graph else 'without'} graph search (c={self.c}, nu={self.nu})"

	def __len__(self):
		return len(self.indices)


class MCTS2(DeepSearcher):
	def __init__(self, net: Model, c: float, nu: float, complete_graph: bool, search_graph: bool, workers=10):
		super().__init__(net)
		# Hyperparameters: c controls exploration and nu controls virtual loss updation us
		self.c = c
		self.nu = nu
		self.complete_graph = complete_graph
		self.search_graph = search_graph
		self.workers = workers

		self.states = dict()
		self.net = net

	@no_grad
	def search(self, state: np.ndarray, time_limit: int, max_states) -> bool:
		self.reset()
		self.tt.tick()

		if Cube.is_solved(state): return True
		# First state is evaluated and expanded individually
		oh = Cube.as_oh(state)
		p, v = self.net(oh)  # Policy and value
		self.states[state.tostring()] = Node(state, p.softmax(dim=1).cpu().numpy().ravel(), float(v.cpu()))
		del p, v

		paths = [deque([])]
		leaves = [self.states[state.tostring()]]
		while self.tt.tock() < time_limit and len(self) < max_states:
			self.tt.profile("Expanding leaves")
			solve_leaf, solve_action = self.expand_leaves(leaves)
			self.tt.end_profile("Expanding leaves")
			if solve_leaf != -1:  # If a solution is found
				self.action_queue = paths[solve_leaf] + deque([solve_action])
				if self.search_graph:
					self._shorten_action_queue()
				return True
			# Gets new paths and leaves to expand from
			paths, leaves = zip(*[self.search_leaf(self.states[state.tostring()], time_limit) for _ in range(self.workers)])

		self.action_queue = paths[solve_leaf] + deque([solve_action])  # Saves an action queue even if it loses which is its best guess
		return False

	def search_leaf(self, node: Node, time_limit: float) -> (list, Node):
		# Finds leaf starting from state
		path = deque()
		self.tt.profile("Exploring next node")
		while not node.is_leaf and self.tt.tock() < time_limit:
			self.tt.profile("sqrt(N)")
			sqrtN = np.sqrt(node.N.sum())
			self.tt.end_profile("sqrt(N)")
			if sqrtN < self.eps:  # Randomly chooses path the first time a state is explored
				self.tt.profile("Random actions")
				action = np.random.choice(Cube.action_dim)
				self.tt.end_profile("Random actions")
			else:
				self.tt.profile("U")
				U = self.c * node.P * sqrtN / (1 + node.N)
				self.tt.end_profile("U")
				self.tt.profile("Q")
				Q = node.W - node.L
				self.tt.end_profile("Q")
				self.tt.profile("Actions")
				g = U + Q
				action = np.argmax(g)
				self.tt.end_profile("Actions")
			self.tt.profile("Update N and L")
			node.N[action] += 1
			node.L[action] += self.nu
			self.tt.end_profile("Update N and L")
			self.tt.profile("Update path")
			path.append(action)
			self.tt.end_profile("Update path")
			self.tt.profile("Get next node")
			node = node.neighs[action]
			self.tt.end_profile("Get next node")
		self.tt.end_profile("Exploring next node")
		return path, node

	def _update_neighbors(self, state: np.ndarray):
		"""
		# Expands around state. If a new state is already in the tree, neighbor relations are updated
		# Assumes that state is already in the tree
		# Used for node expansion
		"""
		state_str = state.tostring()
		new_states = Cube.multi_rotate(
			np.tile(state, (Cube.action_dim, *[1]*len(Cube.get_solved_instance().shape))),
			*Cube.iter_actions()
		)
		new_states_strs = [x.tostring() for x in new_states]
		for i, (new_state, new_state_str) in enumerate(zip(new_states, new_states_strs)):
			self.tt.profile("Ensure neighbors")
			if new_state_str in self.states:
				self.states[state_str].neighs[i] = self.states[new_state_str]
				self.states[new_state_str].neighs[Cube.rev_action(i)] = self.states[state_str]
				if all(self.states[new_state_str].neighs):
					self.states[new_state_str].is_leaf = False
			self.tt.end_profile("Ensure neighbors")
		if all(self.states[state_str].neighs):
			self.states[state_str].is_leaf = False

	def expand_leaves(self, leaves: List[Node]) -> (int, int):
		"""
		Expands all given leaves
		Returns the index of the leaf and the action to solve it
		Both are -1 if no solution is found
		"""
		# print("\nEXPANDING LEAVES")
		# [print(l.state.tolist()) for l in leaves]

		# Explores all new states
		self.tt.profile("Getting new states")
		states = np.array([leaf.state for leaf in leaves])
		new_states = Cube.multi_rotate(np.repeat(states, Cube.action_dim, axis=0), *Cube.iter_actions(len(states)))
		self.tt.end_profile("Getting new states")

		# Checks for solutions
		self.tt.profile("Checking for solved state")
		for i, state in enumerate(new_states):
			if Cube.is_solved(state):
				leaf_idx, action_idx = i // Cube.action_dim, i % Cube.action_dim
				solved_leaf = Node(state, None, None, leaves[leaf_idx], action_idx)
				self.states[state.tostring()] = solved_leaf
				self._update_neighbors(state)
				return i // Cube.action_dim, i % Cube.action_dim
		self.tt.end_profile("Checking for solved state")

		# Gets information about new states
		self.tt.profile("Substate strings")
		new_states_str = [state.tostring() for state in new_states]
		self.tt.end_profile("Substate strings")
		self.tt.profile("One-hot encoding")
		new_states_oh = Cube.as_oh(new_states)
		self.tt.end_profile("One-hot encoding")
		self.tt.profile("Feedforward")
		policies, values = self.net(new_states_oh)
		policies, values = policies.softmax(dim=1).cpu().numpy(), values.squeeze().cpu().numpy()
		self.tt.end_profile("Feedforward")

		self.tt.profile("Generate new states")
		for i, (state, state_str, p, v) in enumerate(zip(new_states, new_states_str, policies, values)):
			leaf_idx, action_idx = i // Cube.action_dim, i % Cube.action_dim
			leaf = leaves[leaf_idx]
			if state_str not in self.states:
				new_leaf = Node(state, p, v, leaf, action_idx)
				self.tt.profile("Update neighbors")
				leaf.neighs[action_idx] = new_leaf
				self.tt.end_profile("Update neighbors")
				self.states[state_str] = new_leaf
				# It is possible to add check for existing neighbors in graph here using self._update_neighbors(state) to ensure graph completeness
				# However, this is so expensive that it has been found to reduce the number of explored states to around a quarter
				# Also, it is not a major problem, as the edges will be updated when new_leaf is expanded, so the problem only exists on the edge of the graph
				# TODO: Test performance difference after implementing this
				# TODO: Save dumb nodes when expanding. This should allow graph completeness without massive overhead
				if self.complete_graph:
					self._update_neighbors(state)

			else:
				self.tt.profile("Update neighbors")
				leaf.neighs[action_idx] = self.states[state_str]
				self.tt.end_profile("Update neighbors")
				self.states[state_str].neighs[Cube.rev_action(action_idx)] = leaf
				if all(self.states[state_str].neighs):
					self.states[state_str].is_leaf = False
			leaf.is_leaf = False
		self.tt.end_profile("Generate new states")

		self.tt.profile("Update W")
		for leaf in leaves:
			max_val = max([x.value for x in leaf.neighs])
			assert max_val != 0
			for action_idx, neighbor in enumerate(leaf.neighs):
				neighbor.W[Cube.rev_action(action_idx)] = max_val
		self.tt.end_profile("Update W")

		return -1, -1

	def _shorten_action_queue(self):
		# TODO: Implement and ensure graph completeness
		# Generates new action queue with BFS through self.states
		pass

	def reset(self):
		super().reset()
		self.states = dict()

	@classmethod
	def from_saved(cls, loc: str, c: float, nu: float, complete_graph: bool, search_graph: bool, workers: int):
		net = Model.load(loc)
		net.to(gpu)
		return cls(net, c=c, nu=nu, complete_graph=complete_graph, search_graph=search_graph, workers=workers)

	def __str__(self):
		return f"Old MCTS {'with' if self.search_graph else 'without'} graph search (c={self.c}, nu={self.nu})"

	def __len__(self):
		return len(self.states)


class AStar(DeepSearcher):

	def __init__(self, net: Model):
		super().__init__(net)
		self.net = net
		self.open = dict()
		self.closed = dict()

	@no_grad
	def search(self, state: np.ndarray, time_limit: float) -> bool:
		# initializing/resetting stuff
		self.tt.tick()
		self.reset()
		self.open = {}
		if Cube.is_solved(state): return True

		oh = Cube.as_oh(state)
		p, v = self.net(oh)  # Policy and value
		self.open[state.tostring()] = {'g_cost': 0, 'h_cost': -float(v.cpu()), 'parent': 'Starting node'}
		del p, v

		# explore leaves
		while self.tt.tock() < time_limit:
			if len(self.open) == 0: # FIXME: bug
				print('AStar open was empty.')
				return False
			# choose current node as the node in open with the lowest f cost
			idx = np.argmin([self.open[node]['g_cost'] + self.open[node]['h_cost'] for node in self.open])
			current = list(self.open)[idx]
			# add to closed and remove from open
			self.closed[current] = self.open[current]
			del self.open[current]
			if self.closed[current]['h_cost'] == 0: return True
			neighbors = self.get_neighbors(current)
			for neighbor in neighbors:
				if neighbor not in self.closed:
					g_cost = self.get_g_cost(current)
					h_cost = self.get_h_cost(neighbor)
					if neighbor not in self.open or g_cost+h_cost < self.open[neighbor]['g_cost']+self.open[neighbor]['h_cost']:
						self.open[neighbor] = {'g_cost': g_cost, 'h_cost': h_cost, 'parent': current}
		return False

	def get_neighbors(self, node: str):
		neighbors = [None] * Cube.action_dim
		node = np.fromstring(node, dtype=Cube.dtype)
		for i in range(Cube.action_dim):
			neighbor = Cube.rotate(node, *Cube.action_space[i])
			neighbors[i] = neighbor.tostring()
		return neighbors

	def get_g_cost(self, node: str):
		return self.closed[node]['g_cost'] + 1

	def get_h_cost(self, node: str):
		node = np.fromstring(node, dtype=Cube.dtype)
		if Cube.is_solved(node):
			return 0
		else:
			oh = Cube.as_oh(node)
			p, v = self.net(oh)
			return -float(v.cpu())

	def __str__(self):
		return f"AStar search"

	def __len__(self): 
		node = list(self.closed)[-1]  
		count = 0
		while True:
			node = self.closed[node]['parent']
			if node == 'Starting node': return count
			count += 1


if __name__ == "__main__":
	seedsetter()
	time_limit = 1
	workers = 2
	net = Model.load("data/local_train")
	searcher = MCTS(net, 0.6, 0.001, False, False, workers)
	log = Logger("local_MCTS.log", "MCTSBOI")
	print = log
	state = np.array("11  5  2 23 14 20  8 17 15  8  3  6  1 21  5 17 19 10 13 22".split(), dtype=Cube.dtype)
	# state, _, _ = Cube.scramble(5)
	print("Found solution:", searcher.search(state, time_limit))
	print("States:", len(searcher))

	searcher2 = MCTS2(net, 0.6, 0.001, False, False, workers)
	print = Logger("local_MCTS2.log", "MCTSBOI2")
	print("Found solution:", searcher2.search(state, time_limit))
	print("States:", len(searcher2))

	# Tests correctness of graph
	print = log
	# Indices
	assert searcher.indices[state.tostring()] == 1
	for s, i in searcher.indices.items():
		assert searcher.states[i].tostring() == s
	assert sorted(searcher.indices.values())[0] == 1
	assert np.all(np.diff(sorted(searcher.indices.values())) == 1)

	used_idcs = np.array(list(searcher.indices.values()))

	# States
	assert np.all(searcher.states[1] == state)
	for i, s in enumerate(searcher.states):
		if i not in used_idcs: continue
		assert s.tostring() in searcher.indices
		assert searcher.indices[s.tostring()] == i

	# Neighbors
	for i, neighs in enumerate(searcher.neighbors):
		if i not in used_idcs: continue
		state = searcher.states[i]
		for j, neighbor_index in enumerate(neighs):
			assert neighbor_index == 0 or neighbor_index in searcher.indices.values()
			if neighbor_index == 0: continue
			substate = Cube.rotate(state, *Cube.action_space[j])
			assert np.all(searcher.states[neighbor_index] == substate)

	# Policy and value
	with torch.no_grad():
		p, v = searcher.net(Cube.as_oh(searcher.states[used_idcs]))
	p, v = p.softmax(dim=1).cpu().numpy(), v.squeeze().cpu().numpy()
	assert np.all(np.isclose(searcher.P[used_idcs], p, atol=1e-5))
	assert np.all(np.isclose(searcher.V[used_idcs], v, atol=1e-5))

	# Leaves
	assert np.all(searcher.neighbors.all(axis=1) != searcher.leaves)

	# W
	for i in used_idcs:
		neighs = searcher.neighbors[i]
		supposed_Ws = np.zeros(Cube.action_dim)
		for j, neighbor_index in enumerate(neighs):
			if neighbor_index == 0: continue
			neighbor_neighbor_indices = searcher.neighbors[neighbor_index]
			if np.all(neighbor_neighbor_indices):
				supposed_Ws[j] = np.max(searcher.V[neighbor_neighbor_indices])
		assert np.all(supposed_Ws == searcher.W[i])


