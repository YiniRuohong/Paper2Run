export type JsonValue = string | number | boolean | null | JsonValue[] | { [key: string]: JsonValue };

export type Session = { id: string; user_id: string; status: string; metadata: Record<string, JsonValue>; created_at: string };
export type Cell = { id: string; session_id: string; status: string; stdout: string; stderr: string; result_summary: string; namespace_hash: string; environment_id: string; artifact_ids: string[]; error_code?: string | null; created_at: string };
export type Action = { sequence_no: number; kind: string; status: string; actor_id: string; cell_id?: string | null; permission_decision: string; created_at: string };
export type Event = { sequence_no: number; type: string; payload: Record<string, JsonValue>; created_at: string };
export type Checkpoint = { id: string; name: string; status: string; action_cursor: number; environment_id: string; reasons: string[]; created_at: string };
export type Recovery = { status: 'recovered' | 'partial' | 'failed'; reasons: string[]; checkpoint_id: string };
export type CheckpointBody = { name: string; action_cursor: number; namespace_snapshot_hash: string; input_artifact_ids: string[]; environment_id: string; replay_plan: Record<string, JsonValue>; manifest_hash: string; manifest: { action_cursor: number; environment_id: string; input_artifact_ids: string[] } };
