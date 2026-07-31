import { useCallback, useEffect, useLayoutEffect, useMemo, useState } from "react";
import { Check, ShieldCheck, Trash2, Users, X } from "lucide-react";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { H2 } from "@nous-research/ui/ui/components/typography/h2";
import { api } from "@/lib/api";
import type { PairingApproveRequest, PairingResponse, PairingUser, ProfileInfo, ProfileRoute } from "@/lib/api";
import { DeleteConfirmDialog } from "@/components/DeleteConfirmDialog";
import { useToast } from "@nous-research/ui/hooks/use-toast";
import { useConfirmDelete } from "@nous-research/ui/hooks/use-confirm-delete";
import { Toast } from "@nous-research/ui/ui/components/toast";
import { Card, CardContent } from "@nous-research/ui/ui/components/card";
import { Input } from "@nous-research/ui/ui/components/input";
import { Checkbox } from "@nous-research/ui/ui/components/checkbox";
import { usePageHeader } from "@/contexts/usePageHeader";

const PROFILE_NAME_RE = /^[a-z0-9][a-z0-9_-]{0,63}$/;
const CREATE_NEW_PROFILE = "__create_new__";
type PairingApprovalScope = "chat" | "topic";

function getUserKey(user: PairingUser): string {
  return `${user.platform}:${user.entry_id || user.user_id}`;
}

function splitUserKey(key: string): { platform: string; user_id: string } {
  const idx = key.indexOf(":");
  if (idx === -1) return { platform: "", user_id: key };
  return { platform: key.slice(0, idx), user_id: key.slice(idx + 1) };
}

function getUserLabel(user: PairingUser): string {
  if (user.subject_type === "chat") {
    return user.chat_name || user.user_name || user.chat_id || user.user_id;
  }
  return user.user_name || user.user_id;
}

function isChatPairing(user: PairingUser): boolean {
  return user.subject_type === "chat";
}

function routeKey(platform: string, chatId: string, threadId = ""): string {
  return `${platform.toLowerCase()}:${chatId}:${threadId}`;
}

export default function PairingPage() {
  const [pending, setPending] = useState<PairingUser[]>([]);
  const [approved, setApproved] = useState<PairingUser[]>([]);
  const [profiles, setProfiles] = useState<ProfileInfo[]>([]);
  const [profileRoutes, setProfileRoutes] = useState<ProfileRoute[]>([]);
  const [loading, setLoading] = useState(true);
  const [approving, setApproving] = useState<string | null>(null);
  const [rejecting, setRejecting] = useState<string | null>(null);
  const [clearing, setClearing] = useState(false);
  const [profileChoice, setProfileChoice] = useState<Record<string, string>>({});
  const [approvalScope, setApprovalScope] = useState<Record<string, PairingApprovalScope>>({});
  const [newProfileName, setNewProfileName] = useState<Record<string, string>>({});
  const [cloneFromDefault, setCloneFromDefault] = useState<Record<string, boolean>>({});
  const { toast, showToast } = useToast();
  const { setEnd } = usePageHeader();

  const profileNames = useMemo(
    () => Array.from(new Set(["default", ...profiles.map((profile) => profile.name)])).filter(Boolean),
    [profiles],
  );

  const routeByChat = useMemo(() => {
    const map = new Map<string, ProfileRoute>();
    for (const route of profileRoutes) {
      map.set(routeKey(route.platform, route.chat_id, route.thread_id || ""), route);
    }
    return map;
  }, [profileRoutes]);

  const loadPairing = useCallback(() => {
    api
      .getPairing()
      .then((res: PairingResponse) => {
        setPending(res.pending);
        setApproved(res.approved);
        setProfileChoice((current) => {
          const next = { ...current };
          for (const item of res.pending) {
            const key = getUserKey(item);
            if (isChatPairing(item) && !next[key]) {
              next[key] = "default";
            }
          }
          return next;
        });
        setApprovalScope((current) => {
          const next = { ...current };
          for (const item of res.pending) {
            const key = getUserKey(item);
            if (isChatPairing(item) && item.thread_id && !next[key]) {
              next[key] = "chat";
            }
          }
          return next;
        });
      })
      .catch(() => showToast("Failed to load pairing requests", "error"))
      .finally(() => setLoading(false));
  }, [showToast]);

  useEffect(() => {
    loadPairing();
    api
      .getProfiles()
      .then((res) => setProfiles(res.profiles))
      .catch(() => showToast("Failed to load profiles", "error"));
    api
      .getProfileRoutes()
      .then((res) => setProfileRoutes(res.routes))
      .catch(() => showToast("Failed to load profile routes", "error"));
  }, [loadPairing]);

  const handleApprove = async (user: PairingUser) => {
    if (!user.entry_id && !user.code) {
      showToast("Missing pairing request id", "error");
      return;
    }
    const key = getUserKey(user);
    const body: PairingApproveRequest = {
      platform: user.platform,
      entry_id: user.entry_id,
      code: user.entry_id ? undefined : user.code,
    };
    if (isChatPairing(user)) {
      body.approval_scope = user.thread_id ? approvalScope[key] || "chat" : "chat";
      const choice = profileChoice[key] || "default";
      if (choice === CREATE_NEW_PROFILE) {
        const name = (newProfileName[key] || "").trim();
        if (!name) {
          showToast("New profile name is required", "error");
          return;
        }
        if (!PROFILE_NAME_RE.test(name)) {
          showToast("Profile names must be lowercase letters, numbers, _ or -", "error");
          return;
        }
        body.profile = name;
        body.create_profile = true;
        body.new_profile_name = name;
        body.clone_from_default = cloneFromDefault[key] !== false;
      } else {
        body.profile = choice;
      }
    }
    setApproving(key);
    try {
      await api.approvePairing(body);
      showToast(`Approved: "${getUserLabel(user)}"`, "success");
      loadPairing();
      api
        .getProfiles()
        .then((res) => setProfiles(res.profiles))
        .catch(() => undefined);
      api
        .getProfileRoutes()
        .then((res) => setProfileRoutes(res.routes))
        .catch(() => undefined);
    } catch (e) {
      showToast(`Error: ${e}`, "error");
    } finally {
      setApproving(null);
    }
  };

  const handleClearPending = async () => {
    if (!window.confirm("Clear all pending pairing requests?")) return;
    setClearing(true);
    try {
      const res = await api.clearPendingPairing();
      showToast(`Cleared ${res.cleared} pending request(s)`, "success");
      loadPairing();
    } catch (e) {
      showToast(`Error: ${e}`, "error");
    } finally {
      setClearing(false);
    }
  };

  const handleReject = async (user: PairingUser) => {
    if (!user.entry_id) {
      showToast("Missing pairing request id", "error");
      return;
    }
    if (!window.confirm(`Reject "${getUserLabel(user)}"?`)) return;
    const key = getUserKey(user);
    setRejecting(key);
    try {
      await api.rejectPairing(user.platform, user.entry_id);
      showToast(`Rejected: "${getUserLabel(user)}"`, "success");
      loadPairing();
    } catch (e) {
      showToast(`Error: ${e}`, "error");
    } finally {
      setRejecting(null);
    }
  };

  const userRevoke = useConfirmDelete({
    onDelete: useCallback(
      async (key: string) => {
        const { platform, user_id } = splitUserKey(key);
        const user = approved.find((u) => getUserKey(u) === key);
        try {
          await api.revokePairing(platform, user_id);
          showToast(
            `Revoked: "${user ? getUserLabel(user) : user_id}"`,
            "success",
          );
          loadPairing();
          api
            .getProfileRoutes()
            .then((res) => setProfileRoutes(res.routes))
            .catch(() => undefined);
        } catch (e) {
          showToast(`Error: ${e}`, "error");
          throw e;
        }
      },
      [approved, loadPairing, showToast],
    ),
  });

  // Put "Clear pending" button in page header
  useLayoutEffect(() => {
    setEnd(
      <Button
        className="uppercase"
        size="sm"
        onClick={handleClearPending}
        disabled={clearing}
        prefix={clearing ? <Spinner /> : <Trash2 className="h-4 w-4" />}
      >
        Clear pending
      </Button>,
    );
    return () => {
      setEnd(null);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [setEnd, clearing]);

  if (loading) {
    return (
      <div className="flex items-center justify-center py-24">
        <Spinner className="text-2xl text-primary" />
      </div>
    );
  }

  const pendingRevokeUser = userRevoke.pendingId
    ? approved.find((u) => getUserKey(u) === userRevoke.pendingId)
    : null;

  return (
    <div className="flex flex-col gap-6">
      <Toast toast={toast} />

      <DeleteConfirmDialog
        open={userRevoke.isOpen}
        onCancel={userRevoke.cancel}
        onConfirm={userRevoke.confirm}
        title="Revoke access"
        description={
          pendingRevokeUser
            ? `"${getUserLabel(pendingRevokeUser)}" will lose access. This cannot be undone.`
            : "This user will lose access. This cannot be undone."
        }
        confirmLabel="Revoke"
        loading={userRevoke.isDeleting}
      />

      {/* Pending requests */}
      <div className="flex flex-col gap-3">
        <H2
          variant="sm"
          className="flex items-center gap-2 text-muted-foreground"
        >
          <Users className="h-4 w-4" />
          Pending requests ({pending.length})
        </H2>

        {pending.length === 0 && (
          <Card>
            <CardContent className="py-8 text-center text-sm text-muted-foreground">
              No pending pairing requests
            </CardContent>
          </Card>
        )}

        {pending.map((user) => {
          const key = getUserKey(user);
          const chatRequest = isChatPairing(user);
          const choice = profileChoice[key] || "default";
          const scope = approvalScope[key] || "chat";
          return (
            <Card key={key}>
              <CardContent className="flex flex-col gap-4 py-4 md:flex-row md:items-start">
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 mb-1">
                    <Badge tone="outline">{user.platform}</Badge>
                    <Badge tone="outline">{chatRequest ? "group" : "user"}</Badge>
                    <span className="font-mono text-sm">{user.code || user.entry_id?.slice(0, 8)}</span>
                  </div>
                  <div className="flex items-center gap-4 text-xs text-muted-foreground">
                    <span className="truncate">{getUserLabel(user)}</span>
                    {chatRequest && user.chat_id && (
                      <span className="truncate">{user.chat_id}</span>
                    )}
                    {chatRequest && user.thread_id && (
                      <span className="truncate">topic id: {user.thread_id}</span>
                    )}
                    {!chatRequest && user.user_name && (
                      <span className="truncate">{user.user_name}</span>
                    )}
                    {chatRequest && user.requester_user_name && (
                      <span className="truncate">requested by {user.requester_user_name}</span>
                    )}
                    {typeof user.age_minutes === "number" && (
                      <span>{user.age_minutes}m ago</span>
                    )}
                  </div>
                  {chatRequest && (
                    <div className="mt-3 flex flex-col gap-2 sm:max-w-md">
                      <select
                        className="h-9 rounded border border-border bg-background px-3 text-sm"
                        value={choice}
                        onChange={(event) => {
                          const value = event.target.value;
                          setProfileChoice((current) => ({ ...current, [key]: value }));
                          if (value === CREATE_NEW_PROFILE) {
                            setCloneFromDefault((current) => ({ ...current, [key]: current[key] ?? true }));
                          }
                        }}
                      >
                        {profileNames.map((name) => (
                          <option key={name} value={name}>{name}</option>
                        ))}
                        <option value={CREATE_NEW_PROFILE}>Create new profile...</option>
                      </select>
                      {user.thread_id && (
                        <div className="grid grid-cols-2 gap-1 rounded border border-border bg-muted/20 p-1 text-xs">
                          {(["chat", "topic"] as const).map((value) => (
                            <button
                              key={value}
                              type="button"
                              aria-pressed={scope === value}
                              className={[
                                "h-8 rounded px-3 font-medium transition-colors",
                                scope === value
                                  ? "bg-primary text-primary-foreground"
                                  : "text-muted-foreground hover:bg-muted hover:text-foreground",
                              ].join(" ")}
                              onClick={() => setApprovalScope((current) => ({
                                ...current,
                                [key]: value,
                              }))}
                            >
                              {value === "chat" ? "Whole chat" : "This topic"}
                            </button>
                          ))}
                        </div>
                      )}
                      {choice === CREATE_NEW_PROFILE && (
                        <div className="flex flex-col gap-2">
                          <Input
                            value={newProfileName[key] || ""}
                            placeholder="new profile name"
                            onChange={(event) => setNewProfileName((current) => ({
                              ...current,
                              [key]: event.target.value,
                            }))}
                          />
                          <label className="flex items-center gap-2 text-xs text-muted-foreground">
                            <Checkbox
                              checked={cloneFromDefault[key] !== false}
                              onCheckedChange={(checked) => setCloneFromDefault((current) => ({
                                ...current,
                                [key]: checked !== false,
                              }))}
                            />
                            Clone default profile
                          </label>
                        </div>
                      )}
                    </div>
                  )}
                </div>

                <div className="flex items-center gap-1 shrink-0">
                  <Button
                    size="sm"
                    className="uppercase border border-border bg-transparent text-muted-foreground hover:text-foreground"
                    onClick={() => handleReject(user)}
                    disabled={rejecting === key || approving === key || !user.entry_id}
                    prefix={
                      rejecting === key ? (
                        <Spinner />
                      ) : (
                        <X className="h-4 w-4" />
                      )
                    }
                  >
                    Reject
                  </Button>
                  <Button
                    size="sm"
                    className="uppercase"
                    onClick={() => handleApprove(user)}
                    disabled={approving === key || rejecting === key || (!user.entry_id && !user.code)}
                    prefix={
                      approving === key ? (
                        <Spinner />
                      ) : (
                        <Check className="h-4 w-4" />
                      )
                    }
                  >
                    Approve
                  </Button>
                </div>
              </CardContent>
            </Card>
          );
        })}
      </div>

      {/* Approved users */}
      <div className="flex flex-col gap-3">
        <H2
          variant="sm"
          className="flex items-center gap-2 text-muted-foreground"
        >
          <ShieldCheck className="h-4 w-4" />
          Approved users ({approved.length})
        </H2>

        {approved.length === 0 && (
          <Card>
            <CardContent className="py-8 text-center text-sm text-muted-foreground">
              No approved users
            </CardContent>
          </Card>
        )}

        {approved.map((user) => {
          const key = getUserKey(user);
          const chatRequest = isChatPairing(user);
          const route = chatRequest && user.chat_id
            ? routeByChat.get(routeKey(user.platform, user.chat_id, user.thread_id || ""))
            : null;
          return (
            <Card key={key}>
              <CardContent className="flex flex-col gap-3 py-4 md:flex-row md:items-start">
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 mb-1">
                    <Badge tone="outline">{user.platform}</Badge>
                    <Badge tone="outline">{chatRequest ? "group" : "user"}</Badge>
                    {route?.profile && (
                      <Badge tone="secondary">profile: {route.profile}</Badge>
                    )}
                    <span className="font-medium text-sm truncate">
                      {getUserLabel(user)}
                    </span>
                  </div>
                  {chatRequest && (
                    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
                      {user.chat_id && <span>chat id: {user.chat_id}</span>}
                      {user.thread_id && <span>topic id: {user.thread_id}</span>}
                      {user.requester_user_name && <span>requested by: {user.requester_user_name}</span>}
                      {!route && <span>no profile route</span>}
                    </div>
                  )}
                  {!chatRequest && user.user_name && (
                    <div className="text-xs text-muted-foreground truncate">
                      {user.user_name}
                    </div>
                  )}
                </div>

                <div className="flex items-center gap-1 shrink-0">
                  <Button
                    size="sm"
                    title="Revoke"
                    aria-label="Revoke"
                    className="uppercase border border-border bg-transparent text-destructive"
                    onClick={() => userRevoke.requestDelete(key)}
                    prefix={<X className="h-4 w-4" />}
                  >
                    Revoke
                  </Button>
                </div>
              </CardContent>
            </Card>
          );
        })}
      </div>
    </div>
  );
}
