import { headers } from "next/headers";
import { Card } from "@aoip/ui-kit";
import { fetchSettings } from "@/lib/settings";
import { SettingsPanel } from "./SettingsPanel";

export default async function ProviderSettingsPage() {
  const cookieHeader = (await headers()).get("cookie") ?? "";
  const result = await fetchSettings(cookieHeader);

  if (result.status === "error") {
    return (
      <Card error>
        <div className="aoip-k err">Không tải được Settings</div>
        <div className="aoip-state" data-testid="settings-error">
          Backend trả mã {result.code || "không phản hồi"}. Thử tải lại trang.
        </div>
      </Card>
    );
  }

  const { tenants, agent_credentials } = result.data;
  return (
    <>
      <div className="aoip-k">Settings — Agent Enrollment</div>
      {tenants.length === 0 ? (
        <div className="aoip-state" data-testid="settings-empty">
          Chưa có tenant nào provision.
        </div>
      ) : (
        <Card>
          <SettingsPanel tenants={tenants} agentCredentials={agent_credentials} />
        </Card>
      )}
    </>
  );
}
