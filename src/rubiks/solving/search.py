from collections import deque
from copy import deepcopy

import numpy as np
import torch

from src.rubiks import cpu, gpu, no_grad
from src.rubiks.model import Model
from src.rubiks.cube.cube import Cube
from src.rubiks.utils.ticktock import TickTock


class Node:
	def __init__(self, state: np.ndarray, policy: np.ndarray, value: float, from_node=None, action_idx: int=None):
		self.is_leaf = True  # When initiated, the node is leaf of search graph
		self.state = state
		self.P = policy
		self.value = value
		# self.neighs[i] is a tuple containing the state obtained by the action Cube.action_space[i]
		# Tuples are used, so they can be used for lookups
		self.neighs = [None] * Cube.action_dim
		self.N = np.zeros(Cube.action_dim)
		self.W = np.zeros(Cube.action_dim)
		self.L = np.zeros(Cube.action_dim)
		if action_idx is not None:
			from_action_idx = Cube.rev_action(action_idx)
			self.neighs[from_action_idx] = from_node
			self.W[from_action_idx] = from_node.value

	def __str__(self):
		return "\n".join([
			"----- Node -----",
			f"Leaf:      {self.is_leaf}",
			f"State:     {tuple(self.state)}",
			f"N:         {self.N}",
			f"W:         {self.W}",
			f"Neighbors: {[id(x) if x is not None else None for x in self.neighs]}",
			"----------------",
		])


class Searcher:
	with_mt = False

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
			if solution_found: return True
		return False

	def _step(self, state: np.ndarray) -> (int, np.ndarray, bool):
		raise NotImplementedError

	def reset(self):
		self.action_queue = deque()
		self.tt.reset()

	def __str__(self):
		raise NotImplementedError


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
	with_mt = True  # TODO: Implement multithreading natively in search method and set to False
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
					return True
				else:
					states[new_tstate] = (tstate, i)
					queue.append(new_state)
		return False

	def __str__(self):
		return "Breadth-first search"


class PolicySearch(DeepSearcher):
	with_mt = not torch.cuda.is_available()

	def __init__(self, net: Model, sample_policy=False):
		super().__init__(net)
		self.sample_policy = sample_policy

	def _step(self, state: np.ndarray) -> (int, np.ndarray, bool):
		policy = torch.nn.functional.softmax(self.net(Cube.as_oh(state).to(gpu), value=False).cpu(), dim=1).numpy().squeeze()
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
	def __init__(self, net: Model, c: float=1, nu: float=0, search_graph=True):
		super().__init__(net)
		#Hyper parameters: c controls exploration and nu controls virtual loss updation us
		self.c = c
		self.nu = nu
		self.search_graf = search_graph

		self.states = dict()
		self.net = net

	@no_grad
	def search(self, state: np.ndarray, time_limit: float) -> bool:
		self.clean_tree()  # Otherwise memory will continue to be used between runs
		self.reset()

		self.tt.tick()
		if Cube.is_solved(state): return True
		#First state is evaluated  and expanded individually
		oh = Cube.as_oh(state).to(gpu)
		p, v = self.net(oh)  # Policy and value
		self.states[state.tostring()] = Node(state, p.cpu().numpy().ravel(), float(v.cpu()))
		del p, v
		self.tt.section("Expanding leaf")
		solve_action = self.expand_leaf(self.states[state.tostring()])
		self.tt.end_section("Expanding leaf")
		if solve_action != -1:
			self.action_queue = deque([solve_action])
			return True
		while self.tt.tock() < time_limit:
			#Continually searching and expanding leaves
			path, leaf = self.search_leaf(self.states[state.tostring()])
			self.tt.section("Expanding leaf")
			solve_action = self.expand_leaf(leaf)
			self.tt.end_section("Expanding leaf")
			if solve_action != -1:
				self.action_queue = path + deque([solve_action])
				if self.search_graf: self._shorten_action_queue()
				return True
		return False

	def search_leaf(self, node: Node) -> (list, Node):
		# Finds leaf starting from state
		path = deque()
		while not node.is_leaf:
			self.tt.section("Exploring next node")
			U = self.c * node.P * np.sqrt(node.N.sum()) / (1 + node.N)
			Q = node.W - node.L
			action = np.argmax(U + Q)
			node.N[action] += 1
			node.L[action] += self.nu
			path.append(action)
			node = node.neighs[action]
			self.tt.end_section("Exploring next node")
		return path, node

	def expand_leaf(self, leaf: Node) -> int:
		# Expands at leaf node and checks if solved state in new states
		# Returns -1 if no action gives solved state else action index

		no_neighs = np.array([i for i in range(Cube.action_dim) if leaf.neighs[i] is None])  # Neighbors that have to be expanded to
		unknown_neighs = list(np.arange(len(no_neighs)))  # Some unknown neighbors may already be known but just not connected
		new_states = np.empty((len(no_neighs), *Cube.get_solved_instance().shape), dtype=Cube.dtype)

		self.tt.section("Exploring child states")
		for i in reversed(range(len(no_neighs))):
			action = no_neighs[i]
			new_states[i] = Cube.rotate(leaf.state, *Cube.action_space[action])
			if Cube.is_solved(new_states[i]): return action

			# If new leaf state is already known, the tree is updated, and the neighbor is no longer considered
			state_str = new_states[i].tostring()
			if state_str in self.states:
				leaf.neighs[action] = self.states[state_str]
				self.states[state_str].neighs[Cube.rev_action(action)] = leaf
				unknown_neighs.pop(i)

		no_neighs = no_neighs[unknown_neighs]
		new_states = new_states[unknown_neighs]
		self.tt.end_section("Exploring child states")

		# Passes new states through net
		self.tt.section("One-hot encoding new states")
		new_states_oh = Cube.as_oh(new_states).to(gpu)
		self.tt.end_section("One-hot encoding new states")
		self.tt.section("Feedforwarding")
		p, v = self.net(new_states_oh)
		p, v = torch.nn.functional.softmax(p.cpu(), dim=1).cpu().numpy(), v.cpu().numpy()
		self.tt.end_section("Feedforwarding")

		self.tt.section("Generate new states")
		for i, action in enumerate(no_neighs):
			new_leaf = Node(new_states[i], p[i], v[i], leaf, action)
			leaf.neighs[action] = new_leaf
			self.states[new_states[i].tostring()] = new_leaf
		self.tt.end_section("Generate new states")

		# Updates W in all non-leaf neighbors
		self.tt.section("Update W")
		max_val = max([x.value for x in leaf.neighs])
		for action, neighbor in enumerate(leaf.neighs):
			if neighbor.is_leaf:
				continue
			neighbor.W[Cube.rev_action(action)] = max_val
		self.tt.end_section("Update W")

		leaf.is_leaf = False
		return -1

	def _shorten_action_queue(self):
		# TODO
		# Generates new action queue with BFS through self.states
		pass

	def clean_tree(self):
		self.states = dict()

	@classmethod
	def from_saved(cls, loc: str, c: float=1, nu: float=1, search_graph=True):
		net = Model.load(loc)
		net.to(gpu)
		return cls(net, c, nu, search_graph)

	def __str__(self):
		return "Monte Carlo Tree Search"


