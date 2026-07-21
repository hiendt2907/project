// Diagnosis detail cho một lượt xử lý — chiếu /trace/{id}/session (đa lượt) và
// /trace/{id}/advisory (một lượt) qua gateway (read-only). Nguồn:
// src/gateway/routes/trace.py (đã sanitize stdout theo head/tail preview,
// KHÔNG exfiltrate nội dung thật — xem comment bảo mật trong file đó).
//
// Đa số lượt xử lý chỉ có MỘT trong hai nguồn (hoặc không có nguồn nào — sự cố
// mức thấp không chạy vòng chẩn đoán sâu): session ưu tiên khi có (vòng chẩn
// đoán đa lượt, chỉ chạy cho sự cố mức critical/high); nếu không có session,
// dùng advisory (một lượt). Không có cả hai là trạng thái bình thường, không
// phải lỗi.
import { fetchGatewayOptional, type GatewaySectionResult } from "@/lib/gateway";

export interface CommandPreview {
  head: string[];
  tail: string[];
  total_lines: number;
  truncated: boolean;
}

export interface CommandResult {
  cmd_id: string;
  command_str: string;
  purpose: string;
  rc: number;
  status: string;
  blocked: boolean;
  block_reason: string;
  preview: CommandPreview;
  stderr_preview: string;
}

export interface DiagnosisTurn {
  turn: number;
  reasoning: string;
  hypothesis: string;
  evidence_gaps: string[];
  confidence: number;
  command_results: CommandResult[];
  diagnosis_complete_claimed: boolean;
}

export interface TraceSession {
  found: boolean;
  trace_id: string;
  total_turns: number;
  degraded: boolean;
  degraded_reason: string;
  turns: DiagnosisTurn[];
  final: {
    root_cause: string;
    blast_radius: string;
    remediation_steps: string[];
    confidence: number;
  };
}

export interface VerificationStep {
  order: number;
  layer: string;
  command: string;
  expected_output: string;
  rationale: string;
}

export interface RemediationStep {
  order: number;
  action: string;
  approval_required: boolean;
  rollback_plan: string;
}

export interface ImpactForecast {
  timeframe: string;
  severity: string;
  prediction: string;
  confidence: string;
}

export interface ImpactChainLink {
  cause: string;
  mechanism: string;
  effect: string;
  evidence_lane: string;
  confidence: string;
}

export interface TraceAdvisory {
  found: boolean;
  trace_id: string;
  advisory?: {
    verdict: string;
    root_cause: string;
    confidence: string;
    affected_workload: string;
    verification_steps: VerificationStep[];
    proposed_remediation: RemediationStep[];
    forecast?: { method: string; basis: string; forecasts: ImpactForecast[] };
    impact_chain?: ImpactChainLink[];
  };
}

export async function fetchTraceSession(traceId: string): Promise<GatewaySectionResult<TraceSession>> {
  return fetchGatewayOptional(`/trace/${encodeURIComponent(traceId)}/session`);
}

export async function fetchTraceAdvisory(traceId: string): Promise<GatewaySectionResult<TraceAdvisory>> {
  return fetchGatewayOptional(`/trace/${encodeURIComponent(traceId)}/advisory`);
}
