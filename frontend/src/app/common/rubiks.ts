// 20x24 reprensentation
export type cube = number[];
// 69 reprensentation. Nice
export type side = number[];
export type cube69 = side[];

export type action = [number, boolean];

export type host = { name: string, address: string };
export type parameter = { [key: string]: number };

export interface IInfoResponse {
	cuda: boolean;
	agents: string[];
	parameters: { [key: string]: parameter };
}

export interface ISolveRequest {
	agentIdx: number;
	timeLimit: number;
	state: cube;
}

export interface ISolveResponse {
	solution: boolean;
	exploredStates: number;
	actions: number[];
}


