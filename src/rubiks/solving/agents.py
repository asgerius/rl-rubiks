import numpy as np
import torch

from src.rubiks.cube.cube import Cube
from src.rubiks.model import Model
from src.rubiks.utils import cpu, gpu
from src.rubiks.solving.search import Searcher, BFS, RandomDFS, MCTS


class Agent:
	# NN based agents see very little gain but much higher compute usage with standard mt implementation
	# TODO: Either stick to ST for these agents or find better solution
	with_mt = False

	def act(self, state: np.ndarray) -> (int, bool):
		raise NotImplementedError

	def __str__(self):
		return f"{self.__class__.__name__}"

class DeepAgent(Agent):
	def __init__(self, net: Model, **kwargs):
		super().__init__(**kwargs)
		self.net = net

	def update_net(self, net):
		self.net = net

	@classmethod
	def from_saved(cls, loc: str, **kwargs):
		net = Model.load(loc)
		net.to(gpu)
		return cls(net, **kwargs)

class TreeAgent(Agent):
	with_mt = True
	def __init__(self, searcher: Searcher, time_limit: int, **kwargs):
		"""
		time_limit: Number of seconds that the tree search part of the algorithm is allowed to searc
		"""
		super().__init__(**kwargs)
		self.searcher = searcher
		self.time_limit = time_limit
		self.has_searched = False

	def act(self, state: np.ndarray) -> (int, bool):
		if not self.searcher.action_queue:
			if self.has_searched: return False
			self.searcher.search(state, self.time_limit)
			self.has_searched = True

		return Cube.action_space[self.searcher.action_queue.popleft()]

class RandomAgent(TreeAgent):
	def __init__(self, time_limit: int,  **kwargs):
		super().__init__(RandomDFS(self), time_limit, **kwargs)

class SimpleBFS(Agent):
	def __init__(self, time_limit: int,  **kwargs):
		super().__init__(BFS(self), time_limit, **kwargs)


class PolicyCube(DeepAgent):
	# Pure neural net agent
	def __init__(self, net, sample_policy=False, **kwargs):
		super().__init__(net, **kwargs)
		self.sample_policy = sample_policy

	def act(self, state: np.ndarray) -> (int, bool):
		# child_states = np.array([Cube.rotate(state, *action) for action in Cube.action_space])
		# oh = Cube.as_oh(child_states).to(gpu)
		oh = Cube.as_oh(state).to(gpu)
		self.net.eval()
		with torch.no_grad():
			policy = self.net(oh, True, False)
			# vals = self.net(oh, False, True).squeeze()
			# print(vals)
			# policy = torch.nn.functional.softmax(self.net(oh, False, True).squeeze(), dim=0)
		if self.sample_policy:
			action = np.random.choice(12, p=policy.cpu().numpy())
		else:
			action = int(torch.argmax(policy))
		return Cube.action_space[action]


class DeepCube(TreeAgent, DeepAgent):
	with_mt = False
	def __init__(self, net, time_limit: int,  **kwargs):
		super().__init__(MCTS(self), time_limit, net, **kwargs)

