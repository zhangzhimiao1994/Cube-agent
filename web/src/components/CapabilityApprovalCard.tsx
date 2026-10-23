import { ShieldCheck } from "lucide-react";
import { useId } from "react";
import "./CapabilityApprovalCard.css";

type Props = {
  approval: { approvalId: string; toolName?: string | null; summary: string };
  decision?: { status: string; decision: string; error?: string };
  sessionAvailable?: boolean;
  sessionUnavailableReason?: string;
  onApprove: () => void;
  onApproveConversation: () => void;
  onReject: () => void;
};

export function CapabilityApprovalCard({ approval, decision, sessionAvailable = false, sessionUnavailableReason,
  onApprove, onApproveConversation, onReject }: Props) {
  const pending = decision?.status === "pending";
  const reasonId = useId();
  return <aside className="capability-approval-card" role="status" aria-label="沙箱权限确认">
    <header>
      <ShieldCheck size={18} aria-hidden="true" />
      <strong>操作审批</strong>
    </header>
    <p className="capability-approval-action">{approval.toolName ?? "待确认操作"}</p>
    <p>{approval.summary}</p>
    <small>审批 {approval.approvalId}</small>
    <div className="capability-approval-actions">
      <button type="button" disabled={pending} onClick={onApprove}>
        {pending && decision.decision === "approve" ? "批准中..." : "批准"}
      </button>
      <button type="button" disabled={pending || !sessionAvailable}
        aria-describedby={!sessionAvailable ? reasonId : undefined} onClick={onApproveConversation}>
        {pending && decision.decision === "approve_session" ? "批准中..." : "本次会话批准"}
      </button>
      <button type="button" disabled={pending} onClick={onReject}>
        {pending && decision.decision === "reject" ? "拒绝中..." : "拒绝"}
      </button>
    </div>
    {!sessionAvailable ? <small id={reasonId}>{sessionUnavailableReason ?? "当前审批不支持会话批准"}</small> : null}
    {decision?.error ? <p role="alert">{decision.error}</p> : null}
  </aside>;
}
