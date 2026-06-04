import { Fragment, useCallback, useEffect, useMemo, useState } from "react";
import {
  ChevronDown,
  ChevronRight,
  Plus,
  RefreshCw,
  Save,
  Settings,
  Trash2,
} from "lucide-react";
import { H2 } from "@nous-research/ui/ui/components/typography/h2";
import { Button } from "@nous-research/ui/ui/components/button";
import { Card, CardContent } from "@nous-research/ui/ui/components/card";
import { Input } from "@nous-research/ui/ui/components/input";
import { Label } from "@nous-research/ui/ui/components/label";
import { Checkbox } from "@nous-research/ui/ui/components/checkbox";
import { api } from "@/lib/api";
import type {
  DiscoveredProfileRoute,
  ProfileChatSetting,
  ProfileInfo,
  ProfileRoute,
  ProfileRoutesResponse,
} from "@/lib/api";

type Props = {
  profiles: ProfileInfo[];
};

type ChatTopicGroup = {
  id: string;
  platform: string;
  chat_id: string;
  chatLabel: string;
  chatType: string;
  chatItem: DiscoveredProfileRoute;
  topics: DiscoveredProfileRoute[];
};

const INHERIT_PROFILE = "__inherit__";
const CHAT_SETTING_KEYS = [
  "response_mode",
  "transcribe_audio",
  "reply_to_mode",
  "tool_progress",
  "show_reasoning",
  "tool_preview_length",
  "interim_assistant_messages",
  "long_running_notifications",
  "busy_ack_detail",
  "cleanup_progress",
  "streaming",
  "gateway_restart_notification",
] as const;
const DEFAULT_CHAT_SETTINGS: ProfileChatSetting = {
  response_mode: "default",
  transcribe_audio: "default",
  reply_to_mode: "default",
  tool_progress: "default",
  show_reasoning: "default",
  tool_preview_length: "default",
  interim_assistant_messages: "default",
  long_running_notifications: "default",
  busy_ack_detail: "default",
  cleanup_progress: "default",
  streaming: "default",
  gateway_restart_notification: "default",
};

const CHAT_SETTING_LABELS: Record<(typeof CHAT_SETTING_KEYS)[number], string> = {
  response_mode: "Response",
  transcribe_audio: "Audio",
  reply_to_mode: "Replies",
  tool_progress: "Tools",
  show_reasoning: "Reasoning",
  tool_preview_length: "Preview",
  interim_assistant_messages: "Interim",
  long_running_notifications: "Long runs",
  busy_ack_detail: "Busy detail",
  cleanup_progress: "Cleanup",
  streaming: "Streaming",
  gateway_restart_notification: "Lifecycle",
};

const CHAT_SETTING_VALUE_LABELS: Record<string, string> = {
  all: "all",
  mentions: "mentions",
  on: "on",
  off: "off",
  first: "first",
  new: "new",
  verbose: "verbose",
};

function emptyRoute(defaultProfile: string): ProfileRoute {
  return {
    id: `route-${Date.now()}`,
    enabled: true,
    platform: "telegram",
    chat_id: "",
    thread_id: "",
    profile: defaultProfile || "default",
    label: "",
  };
}

function routeIdFor(item: Pick<ProfileRoute, "platform" | "chat_id" | "thread_id" | "profile">): string {
  const topic = item.thread_id?.trim() || "chat";
  return `${item.platform}-${item.chat_id}-${topic}-${item.profile}`.replace(/[^a-zA-Z0-9_-]+/g, "-");
}

function sameAssignment(route: ProfileRoute, item: Pick<DiscoveredProfileRoute, "platform" | "chat_id" | "thread_id">): boolean {
  return (
    route.platform === item.platform &&
    route.chat_id === item.chat_id &&
    (route.thread_id || "") === (item.thread_id || "")
  );
}

function matchLabel(matchType: string): string {
  if (matchType === "topic") return "topic override";
  if (matchType === "chat") return "chat rule";
  return "global default";
}

function splitCompoundLabel(label: string): [string, string] | null {
  const parts = label.split(" / ");
  if (parts.length < 2) return null;
  return [parts[0].trim(), parts.slice(1).join(" / ").trim()];
}

function chatLabelForItem(item: DiscoveredProfileRoute): string {
  if (item.chat_name?.trim()) return item.chat_name.trim();
  if (!item.thread_id && item.label?.trim()) return item.label.trim();
  const compound = splitCompoundLabel(item.label || "");
  if (compound?.[0]) return compound[0];
  return item.chat_id || "Unknown chat";
}

function topicLabelForItem(item: DiscoveredProfileRoute, chatLabel: string): string {
  if (item.chat_topic?.trim()) return item.chat_topic.trim();
  const raw = item.label?.trim() || "";
  const compound = splitCompoundLabel(raw);
  if (compound && compound[0] === chatLabel && compound[1]) return compound[1];
  if (raw && raw !== chatLabel) return raw;
  return item.thread_id ? `topic ${item.thread_id}` : "chat";
}

function routeLabelForAssignment(item: DiscoveredProfileRoute): string {
  const chatLabel = chatLabelForItem(item);
  return item.thread_id ? topicLabelForItem(item, chatLabel) : chatLabel;
}

function directRoute(
  routes: ProfileRoute[],
  item: Pick<DiscoveredProfileRoute, "platform" | "chat_id" | "thread_id">,
): ProfileRoute | undefined {
  return routes.find((route) => route.enabled !== false && sameAssignment(route, item));
}

function chatSettingKey(platform: string, chatId: string): string {
  return `${platform}:${chatId}`;
}

function defaultChatSetting(group: ChatTopicGroup): ProfileChatSetting {
  return {
    ...DEFAULT_CHAT_SETTINGS,
    id: `${group.platform}:${group.chat_id}:`,
    platform: group.platform,
    chat_id: group.chat_id,
    label: group.chatLabel,
  };
}

function settingHasOverrides(setting: ProfileChatSetting): boolean {
  return CHAT_SETTING_KEYS.some((key) => {
    const value = setting[key];
    return value !== undefined && value !== "default" && value !== "";
  });
}

function chatSettingDiffs(setting: ProfileChatSetting, defaults: ProfileChatSetting): string[] {
  return CHAT_SETTING_KEYS.flatMap((key) => {
    const value = setting[key];
    if (value === undefined || value === "" || value === "default") return [];
    const defaultValue = defaults[key] ?? "default";
    if (value === defaultValue) return [];
    const formatted = CHAT_SETTING_VALUE_LABELS[String(value)] || String(value);
    return [`${CHAT_SETTING_LABELS[key]}: ${formatted}`];
  });
}

type SettingsTarget =
  | { kind: "defaults" }
  | { kind: "chat"; group: ChatTopicGroup };

export function ProfileRouteEditor({ profiles }: Props) {
  const [data, setData] = useState<ProfileRoutesResponse>({ default_profile: "default", routes: [] });
  const [discovered, setDiscovered] = useState<DiscoveredProfileRoute[]>([]);
  const [chatDefaults, setChatDefaults] = useState<ProfileChatSetting>(DEFAULT_CHAT_SETTINGS);
  const [chatSettings, setChatSettings] = useState<ProfileChatSetting[]>([]);
  const [expandedChats, setExpandedChats] = useState<Record<string, boolean>>({});
  const [showAllTopics, setShowAllTopics] = useState<Record<string, boolean>>({});
  const [settingsTarget, setSettingsTarget] = useState<SettingsTarget | null>(null);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const [loadingKnown, setLoadingKnown] = useState(false);

  const profileNames = useMemo(
    () => Array.from(new Set(["default", ...profiles.map((profile) => profile.name)])).filter(Boolean),
    [profiles],
  );

  const discoveredGroups = useMemo<ChatTopicGroup[]>(() => {
    type DraftGroup = Omit<ChatTopicGroup, "chatItem"> & { chatItem?: DiscoveredProfileRoute };

    const groups = new Map<string, DraftGroup>();
    for (const item of discovered) {
      const platform = item.platform || "telegram";
      const chatId = item.chat_id || "";
      if (!chatId) continue;

      const groupId = chatSettingKey(platform, chatId);
      const itemChatLabel = chatLabelForItem(item);
      let group = groups.get(groupId);
      if (!group) {
        group = {
          id: groupId,
          platform,
          chat_id: chatId,
          chatLabel: itemChatLabel,
          chatType: item.chat_type || "",
          topics: [],
        };
        groups.set(groupId, group);
      }

      if (item.chat_name?.trim()) group.chatLabel = item.chat_name.trim();
      if (!group.chatType && item.chat_type) group.chatType = item.chat_type;

      if (item.thread_id) {
        group.topics.push(item);
      } else {
        group.chatItem = item;
      }
    }

    for (const route of data.routes) {
      if (!route.chat_id?.trim()) continue;
      const platform = route.platform || "telegram";
      const chatId = route.chat_id;
      const groupId = chatSettingKey(platform, chatId);
      const label = route.label?.trim() || chatId;
      const item: DiscoveredProfileRoute = {
        id: `${groupId}${route.thread_id || "chat"}:configured`,
        source: "configured",
        platform,
        chat_id: chatId,
        chat_name: route.thread_id ? "" : label,
        chat_type: "",
        thread_id: route.thread_id || "",
        chat_topic: route.thread_id ? label : "",
        label,
        direct_profile: route.profile,
        effective_profile: route.profile,
        match_type: route.thread_id ? "topic" : "chat",
        route_id: route.id,
        enabled: route.enabled,
        updated_at: "",
      };
      let group = groups.get(groupId);
      if (!group) {
        group = {
          id: groupId,
          platform,
          chat_id: chatId,
          chatLabel: route.thread_id ? chatId : label,
          chatType: "",
          topics: [],
        };
        groups.set(groupId, group);
      }
      if (route.thread_id) {
        if (!group.topics.some((existing) => sameAssignment({ ...route, enabled: true }, existing))) {
          group.topics.push(item);
        }
      } else if (!group.chatItem || group.chatItem.source === "derived") {
        group.chatItem = item;
        group.chatLabel = label;
      }
    }

    return Array.from(groups.values())
      .map((group) => {
        const chatItem: DiscoveredProfileRoute = group.chatItem || {
          id: `${group.id}:chat`,
          source: "derived",
          platform: group.platform,
          chat_id: group.chat_id,
          chat_name: group.chatLabel,
          chat_type: group.chatType,
          thread_id: "",
          chat_topic: "",
          label: group.chatLabel,
          direct_profile: "",
          effective_profile: data.default_profile || "default",
          match_type: "default",
          route_id: "",
          enabled: true,
          updated_at: "",
        };
        return {
          ...group,
          chatItem,
          topics: [...group.topics].sort((a, b) => (
            topicLabelForItem(a, group.chatLabel).localeCompare(topicLabelForItem(b, group.chatLabel))
            || (a.thread_id || "").localeCompare(b.thread_id || "", undefined, { numeric: true })
          )),
        };
      })
      .sort((a, b) => (
        a.chatLabel.localeCompare(b.chatLabel)
        || a.platform.localeCompare(b.platform)
        || a.chat_id.localeCompare(b.chat_id)
      ));
  }, [data.default_profile, data.routes, discovered]);

  const chatSettingsByKey = useMemo(() => {
    const map = new Map<string, ProfileChatSetting>();
    for (const setting of chatSettings) {
      if (!setting.platform || !setting.chat_id) continue;
      map.set(chatSettingKey(setting.platform, setting.chat_id), setting);
    }
    return map;
  }, [chatSettings]);

  const manualDraftRoutes = useMemo(
    () => data.routes
      .map((route, index) => ({ route, index }))
      .filter(({ route }) => !route.chat_id?.trim()),
    [data.routes],
  );

  const loadRoutes = useCallback(async () => {
    const [routes, known, settings] = await Promise.all([
      api.getProfileRoutes(),
      api.getDiscoveredProfileRoutes(),
      api.getProfileChatSettings(),
    ]);
    setData(routes);
    setDiscovered(known.items);
    setChatDefaults({ ...DEFAULT_CHAT_SETTINGS, ...(settings.defaults || {}) });
    setChatSettings(settings.settings);
  }, []);

  useEffect(() => {
    setLoadingKnown(true);
    loadRoutes()
      .catch((err) => setError(String((err as Error).message || err)))
      .finally(() => setLoadingKnown(false));
  }, [loadRoutes]);

  function updateRoute(index: number, patch: Partial<ProfileRoute>) {
    setData((current) => ({
      ...current,
      routes: current.routes.map((route, i) => (i === index ? { ...route, ...patch } : route)),
    }));
  }

  function removeRoute(index: number) {
    setData((current) => ({
      ...current,
      routes: current.routes.filter((_, i) => i !== index),
    }));
  }

  function setRouteProfile(item: DiscoveredProfileRoute, profile: string, inheritedProfile?: string) {
    setData((current) => {
      const shouldInherit = profile === INHERIT_PROFILE || profile === inheritedProfile;
      if (shouldInherit) {
        return {
          ...current,
          routes: current.routes.filter((route) => !sameAssignment(route, item)),
        };
      }

      const nextRoute: ProfileRoute = {
        id: routeIdFor({ ...item, profile }),
        enabled: true,
        platform: item.platform,
        chat_id: item.chat_id,
        thread_id: item.thread_id || "",
        profile,
        label: routeLabelForAssignment(item),
      };
      const existingIndex = current.routes.findIndex((route) => sameAssignment(route, item));
      if (existingIndex === -1) {
        return { ...current, routes: [...current.routes, nextRoute] };
      }
      return {
        ...current,
        routes: current.routes.map((route, index) => (
          index === existingIndex
            ? { ...route, enabled: true, profile, label: route.label || nextRoute.label }
            : route
        )),
      };
    });
  }

  function chatProfile(group: ChatTopicGroup): string {
    return directRoute(data.routes, group.chatItem)?.profile || data.default_profile || "default";
  }

  function topicDirectProfile(item: DiscoveredProfileRoute): string {
    return directRoute(data.routes, item)?.profile || "";
  }

  function topicProfile(item: DiscoveredProfileRoute, group: ChatTopicGroup): string {
    return topicDirectProfile(item) || chatProfile(group);
  }

  function topicOverridesChat(item: DiscoveredProfileRoute, group: ChatTopicGroup): boolean {
    const directProfile = topicDirectProfile(item);
    return Boolean(directProfile && directProfile !== chatProfile(group));
  }

  function currentChatSetting(group: ChatTopicGroup): ProfileChatSetting {
    return chatSettingsByKey.get(group.id) || defaultChatSetting(group);
  }

  function updateChatSetting(group: ChatTopicGroup, patch: Partial<ProfileChatSetting>) {
    setChatSettings((current) => {
      const key = group.id;
      const nextSetting = { ...defaultChatSetting(group), ...chatSettingsByKey.get(key), ...patch };
      const shouldKeep = settingHasOverrides(nextSetting);
      const without = current.filter((setting) => chatSettingKey(String(setting.platform || ""), String(setting.chat_id || "")) !== key);
      return shouldKeep ? [...without, nextSetting] : without;
    });
  }

  function updateDefaultChatSetting(patch: Partial<ProfileChatSetting>) {
    setChatDefaults((current) => ({ ...DEFAULT_CHAT_SETTINGS, ...current, ...patch }));
  }

  async function save() {
    setSaving(true);
    setError("");
    try {
      const [saved] = await Promise.all([
        api.updateProfileRoutes(data),
        api.updateProfileChatSettings({ defaults: chatDefaults, settings: chatSettings }),
      ]);
      setData({ default_profile: saved.default_profile, routes: saved.routes });
      const [known, settings] = await Promise.all([
        api.getDiscoveredProfileRoutes(),
        api.getProfileChatSettings(),
      ]);
      setDiscovered(known.items);
      setChatDefaults({ ...DEFAULT_CHAT_SETTINGS, ...(settings.defaults || {}) });
      setChatSettings(settings.settings);
    } catch (err) {
      setError(String((err as Error).message || err));
    } finally {
      setSaving(false);
    }
  }

  function renderChatSettingsFields(
    setting: ProfileChatSetting,
    onChange: (patch: Partial<ProfileChatSetting>) => void,
  ) {
    const renderTriStateOptions = () => (
      <>
        <option value="default">Default</option>
        <option value="on">On</option>
        <option value="off">Off</option>
      </>
    );

    return (
      <>
        <Label className="grid gap-1 text-xs text-muted-foreground">
          Response mode
          <select
            className="h-9 border border-input bg-transparent px-2 text-sm text-foreground"
            value={setting.response_mode}
            onChange={(event) => onChange({ response_mode: event.target.value as ProfileChatSetting["response_mode"] })}
          >
            <option value="default">Default</option>
            <option value="all">Respond to all messages</option>
            <option value="mentions">Only mentions and commands</option>
          </select>
        </Label>

        <Label className="grid gap-1 text-xs text-muted-foreground">
          Audio transcription
          <select
            className="h-9 border border-input bg-transparent px-2 text-sm text-foreground"
            value={setting.transcribe_audio}
            onChange={(event) => onChange({ transcribe_audio: event.target.value as ProfileChatSetting["transcribe_audio"] })}
          >
            <option value="default">Default</option>
            <option value="on">Transcribe voice and audio</option>
            <option value="off">Do not transcribe audio</option>
          </select>
        </Label>

        <Label className="grid gap-1 text-xs text-muted-foreground">
          Reply threading
          <select
            className="h-9 border border-input bg-transparent px-2 text-sm text-foreground"
            value={setting.reply_to_mode}
            onChange={(event) => onChange({ reply_to_mode: event.target.value as ProfileChatSetting["reply_to_mode"] })}
          >
            <option value="default">Default</option>
            <option value="off">Do not reply-anchor</option>
            <option value="first">First chunk only</option>
            <option value="all">All chunks</option>
          </select>
        </Label>

        <Label className="grid gap-1 text-xs text-muted-foreground">
          Tool progress
          <select
            className="h-9 border border-input bg-transparent px-2 text-sm text-foreground"
            value={setting.tool_progress}
            onChange={(event) => onChange({ tool_progress: event.target.value as ProfileChatSetting["tool_progress"] })}
          >
            <option value="default">Default</option>
            <option value="off">Off</option>
            <option value="new">New tools only</option>
            <option value="all">All tools</option>
            <option value="verbose">Verbose</option>
          </select>
        </Label>

        <Label className="grid gap-1 text-xs text-muted-foreground">
          Show reasoning
          <select
            className="h-9 border border-input bg-transparent px-2 text-sm text-foreground"
            value={setting.show_reasoning}
            onChange={(event) => onChange({ show_reasoning: event.target.value as ProfileChatSetting["show_reasoning"] })}
          >
            {renderTriStateOptions()}
          </select>
        </Label>

        <Label className="grid gap-1 text-xs text-muted-foreground">
          Tool preview length
          <Input
            type="number"
            min={0}
            placeholder="Default"
            value={setting.tool_preview_length === "default" ? "" : String(setting.tool_preview_length ?? "")}
            onChange={(event) => onChange({
              tool_preview_length: event.target.value === "" ? "default" : Number(event.target.value),
            })}
          />
        </Label>

        <Label className="grid gap-1 text-xs text-muted-foreground">
          Interim assistant messages
          <select
            className="h-9 border border-input bg-transparent px-2 text-sm text-foreground"
            value={setting.interim_assistant_messages}
            onChange={(event) => onChange({ interim_assistant_messages: event.target.value as ProfileChatSetting["interim_assistant_messages"] })}
          >
            {renderTriStateOptions()}
          </select>
        </Label>

        <Label className="grid gap-1 text-xs text-muted-foreground">
          Long-running notifications
          <select
            className="h-9 border border-input bg-transparent px-2 text-sm text-foreground"
            value={setting.long_running_notifications}
            onChange={(event) => onChange({ long_running_notifications: event.target.value as ProfileChatSetting["long_running_notifications"] })}
          >
            {renderTriStateOptions()}
          </select>
        </Label>

        <Label className="grid gap-1 text-xs text-muted-foreground">
          Busy acknowledgement detail
          <select
            className="h-9 border border-input bg-transparent px-2 text-sm text-foreground"
            value={setting.busy_ack_detail}
            onChange={(event) => onChange({ busy_ack_detail: event.target.value as ProfileChatSetting["busy_ack_detail"] })}
          >
            {renderTriStateOptions()}
          </select>
        </Label>

        <Label className="grid gap-1 text-xs text-muted-foreground">
          Cleanup progress messages
          <select
            className="h-9 border border-input bg-transparent px-2 text-sm text-foreground"
            value={setting.cleanup_progress}
            onChange={(event) => onChange({ cleanup_progress: event.target.value as ProfileChatSetting["cleanup_progress"] })}
          >
            {renderTriStateOptions()}
          </select>
        </Label>

        <Label className="grid gap-1 text-xs text-muted-foreground">
          Streaming
          <select
            className="h-9 border border-input bg-transparent px-2 text-sm text-foreground"
            value={setting.streaming}
            onChange={(event) => onChange({ streaming: event.target.value as ProfileChatSetting["streaming"] })}
          >
            {renderTriStateOptions()}
          </select>
        </Label>

        <Label className="grid gap-1 text-xs text-muted-foreground">
          Gateway lifecycle notifications
          <select
            className="h-9 border border-input bg-transparent px-2 text-sm text-foreground"
            value={setting.gateway_restart_notification}
            onChange={(event) => onChange({ gateway_restart_notification: event.target.value as ProfileChatSetting["gateway_restart_notification"] })}
          >
            {renderTriStateOptions()}
          </select>
        </Label>
      </>
    );
  }

  return (
    <Card>
      <CardContent className="flex flex-col gap-5 py-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <H2 variant="sm" className="text-muted-foreground">Assigned Chats And Topics</H2>
          <div className="flex flex-wrap gap-2">
            <Button
              type="button"
              ghost
              size="sm"
              onClick={() => {
                setLoadingKnown(true);
                loadRoutes()
                  .catch((err) => setError(String((err as Error).message || err)))
                  .finally(() => setLoadingKnown(false));
              }}
            >
              <RefreshCw className="h-4 w-4" />
              Refresh
            </Button>
            <Button type="button" size="sm" onClick={() => setData((current) => ({ ...current, routes: [...current.routes, emptyRoute(current.default_profile)] }))}>
              <Plus className="h-4 w-4" />
              Add assignment
            </Button>
          </div>
        </div>

        {error ? <div className="text-sm text-destructive">{error}</div> : null}

        <div className="flex flex-wrap items-end gap-3">
          <Label className="grid min-w-[260px] max-w-xs gap-1 text-xs text-muted-foreground">
            Default profile
            <select
              className="h-9 border border-input bg-transparent px-2 text-sm text-foreground"
              value={data.default_profile}
              onChange={(event) => setData((current) => ({ ...current, default_profile: event.target.value }))}
            >
              {profileNames.map((name) => (
                <option key={name} value={name}>{name}</option>
              ))}
            </select>
          </Label>
          <Button
            type="button"
            ghost
            size="sm"
            onClick={() => setSettingsTarget({ kind: "defaults" })}
            title="Default settings for all chats"
          >
            <Settings className={settingHasOverrides(chatDefaults) ? "h-4 w-4 text-primary" : "h-4 w-4"} />
            Default chat settings
          </Button>
        </div>

        <div className="flex flex-col gap-2">
          <div className="text-xs font-mondwest text-display tracking-wider text-muted-foreground">Known chats and topics</div>
          <div className="overflow-x-auto border border-border">
            <table className="w-full min-w-[940px] text-sm">
              <thead className="text-xs text-muted-foreground">
                <tr>
                  <th className="px-3 py-2 text-left">Chat / Topic</th>
                  <th className="px-3 py-2 text-left">Platform / IDs</th>
                  <th className="px-3 py-2 text-left">Profile</th>
                  <th className="px-3 py-2 text-left">Rule</th>
                  <th className="px-3 py-2 text-right">Settings</th>
                </tr>
              </thead>
              <tbody>
                {loadingKnown ? (
                  <tr><td className="px-3 py-4 text-muted-foreground" colSpan={5}>Loading known chats...</td></tr>
                ) : discoveredGroups.length === 0 && manualDraftRoutes.length === 0 ? (
                  <tr><td className="px-3 py-4 text-muted-foreground" colSpan={5}>No known chats yet. Send a message to the bot from a chat or topic, then refresh.</td></tr>
                ) : (<>
                  {manualDraftRoutes.map(({ route, index }) => (
                    <tr key={route.id || index} className="border-t border-border bg-muted/10 align-top">
                      <td className="px-3 py-2">
                        <div className="grid gap-2">
                          <Input
                            value={route.label || ""}
                            placeholder="Label"
                            onChange={(event) => updateRoute(index, { label: event.target.value })}
                          />
                          <div className="text-xs text-muted-foreground">Manual chat or topic assignment</div>
                        </div>
                      </td>
                      <td className="px-3 py-2">
                        <div className="grid gap-2">
                          <Input
                            value={route.platform}
                            placeholder="Platform"
                            onChange={(event) => updateRoute(index, { platform: event.target.value })}
                          />
                          <Input
                            value={route.chat_id}
                            placeholder="Chat ID"
                            onChange={(event) => updateRoute(index, { chat_id: event.target.value })}
                          />
                          <Input
                            value={route.thread_id || ""}
                            placeholder="Topic ID, optional"
                            onChange={(event) => updateRoute(index, { thread_id: event.target.value })}
                          />
                        </div>
                      </td>
                      <td className="px-3 py-2">
                        <select
                          className="h-9 w-full border border-input bg-transparent px-2 text-sm text-foreground"
                          value={route.profile}
                          onChange={(event) => updateRoute(index, { profile: event.target.value })}
                        >
                          {profileNames.map((name) => (
                            <option key={name} value={name}>{name}</option>
                          ))}
                        </select>
                      </td>
                      <td className="px-3 py-2">
                        <Label className="flex items-center gap-2 text-xs text-muted-foreground">
                          <Checkbox checked={route.enabled !== false} onCheckedChange={(checked) => updateRoute(index, { enabled: checked === true })} />
                          Enabled
                        </Label>
                      </td>
                      <td className="px-3 py-2 text-right">
                        <Button type="button" ghost size="icon" onClick={() => removeRoute(index)} aria-label="Remove assignment">
                          <Trash2 className="h-4 w-4 text-destructive" />
                        </Button>
                      </td>
                    </tr>
                  ))}
                  {discoveredGroups.map((group, groupIndex) => {
                  const profile = chatProfile(group);
                  const chatDirect = directRoute(data.routes, group.chatItem);
                  const overrideTopics = group.topics.filter((item) => topicOverridesChat(item, group));
                  const inheritedTopics = group.topics.length - overrideTopics.length;
                  const expanded = expandedChats[group.id] ?? overrideTopics.length > 0;
                  const showAll = showAllTopics[group.id] === true;
                  const visibleTopics = showAll ? group.topics : overrideTopics;
                  const hiddenTopics = group.topics.length - visibleTopics.length;
                  const setting = currentChatSetting(group);
                  const settingDiffs = chatSettingDiffs(setting, chatDefaults);
                  const rowTone = groupIndex % 2 === 0 ? "bg-muted/10" : "bg-muted/[0.03]";
                  const topicTone = groupIndex % 2 === 0 ? "bg-muted/[0.04]" : "bg-transparent";

                  return (
                    <Fragment key={group.id}>
                      <tr className={`border-t-2 border-border align-top hover:bg-muted/20 ${rowTone}`}>
                        <td className="border-l-4 border-primary/50 px-3 py-2">
                          <div className="flex items-start gap-2">
                            {group.topics.length ? (
                              <Button
                                type="button"
                                ghost
                                size="icon"
                                className="h-7 w-7 shrink-0"
                                onClick={() => setExpandedChats((current) => ({ ...current, [group.id]: !expanded }))}
                                aria-label={expanded ? "Hide topics" : "Show topics"}
                              >
                                {expanded ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
                              </Button>
                            ) : <span className="h-7 w-7 shrink-0" />}
                            <div>
                              <div className="font-medium">{group.chatLabel}</div>
                              <div className="text-xs text-muted-foreground">
                                Chat
                                {group.topics.length ? ` · ${group.topics.length} topics` : ""}
                                {overrideTopics.length ? ` · ${overrideTopics.length} overrides` : ""}
                              </div>
                              {settingDiffs.length ? (
                                <div className="mt-2 flex flex-wrap gap-1">
                                  {settingDiffs.slice(0, 4).map((diff) => (
                                    <span key={diff} className="border border-primary/30 bg-primary/10 px-1.5 py-0.5 text-[11px] text-primary">
                                      {diff}
                                    </span>
                                  ))}
                                  {settingDiffs.length > 4 ? (
                                    <span className="border border-border px-1.5 py-0.5 text-[11px] text-muted-foreground">
                                      +{settingDiffs.length - 4}
                                    </span>
                                  ) : null}
                                </div>
                              ) : null}
                            </div>
                          </div>
                        </td>
                        <td className="px-3 py-2">
                          <div>{group.platform}</div>
                          <div className="font-mono text-xs text-muted-foreground">{group.chat_id}</div>
                        </td>
                        <td className="px-3 py-2">
                          <select
                            className="h-9 w-full border border-input bg-transparent px-2 text-sm text-foreground"
                            value={profile}
                            onChange={(event) => setRouteProfile(group.chatItem, event.target.value, data.default_profile)}
                          >
                            {profileNames.map((name) => (
                              <option key={name} value={name}>{name}</option>
                            ))}
                          </select>
                        </td>
                        <td className="px-3 py-2">
                          <div>{chatDirect ? "chat rule" : matchLabel(group.chatItem.match_type)}</div>
                          {inheritedTopics > 0 ? (
                            <div className="text-xs text-muted-foreground">{inheritedTopics} inherited topics</div>
                          ) : null}
                        </td>
                        <td className="px-3 py-2 text-right">
                          <Button
                            type="button"
                            ghost
                            size="icon"
                            aria-label={`Settings for ${group.chatLabel}`}
                            title={`Settings for ${group.chatLabel}`}
                            onClick={() => setSettingsTarget({ kind: "chat", group })}
                          >
                            <Settings className={settingHasOverrides(setting) ? "h-4 w-4 text-primary" : "h-4 w-4"} />
                          </Button>
                        </td>
                      </tr>

                      {expanded && visibleTopics.map((item) => {
                        const directProfile = topicDirectProfile(item);
                        const selected = directProfile && directProfile !== profile ? directProfile : INHERIT_PROFILE;
                        const topicName = topicLabelForItem(item, group.chatLabel);
                        return (
                          <tr key={item.id} className={`border-t border-border/70 align-top hover:bg-muted/15 ${topicTone}`}>
                            <td className="border-l-4 border-primary/20 px-3 py-2 pl-12">
                              <div className="flex items-start gap-2">
                                <span className="mt-2 h-px w-4 shrink-0 bg-border" aria-hidden="true" />
                                <div>
                                  <div className="font-medium">{topicName}</div>
                                  <div className="font-mono text-xs text-muted-foreground">topic id: {item.thread_id || "-"}</div>
                                </div>
                              </div>
                            </td>
                            <td className="px-3 py-2 text-muted-foreground">-</td>
                            <td className="px-3 py-2">
                              <select
                                className="h-9 w-full border border-input bg-transparent px-2 text-sm text-foreground"
                                value={selected}
                                onChange={(event) => setRouteProfile(item, event.target.value, profile)}
                              >
                                <option value={INHERIT_PROFILE}>Inherit chat profile ({profile})</option>
                                {profileNames.map((name) => (
                                  <option key={name} value={name}>{name}</option>
                                ))}
                              </select>
                            </td>
                            <td className="px-3 py-2">
                              <div>{selected === INHERIT_PROFILE ? "inherits chat" : "topic override"}</div>
                              <div className="text-xs text-muted-foreground">{topicProfile(item, group)}</div>
                            </td>
                            <td className="px-3 py-2" />
                          </tr>
                        );
                      })}

                      {expanded && group.topics.length > 0 && hiddenTopics > 0 ? (
                        <tr className={`border-t border-border/70 ${topicTone}`}>
                          <td className="border-l-4 border-primary/20 px-3 py-2 pl-12 text-sm text-muted-foreground" colSpan={5}>
                            <Button
                              type="button"
                              ghost
                              size="sm"
                              onClick={() => setShowAllTopics((current) => ({ ...current, [group.id]: true }))}
                            >
                              Show all topics ({hiddenTopics} inherited)
                            </Button>
                          </td>
                        </tr>
                      ) : null}

                      {expanded && showAll && overrideTopics.length > 0 && hiddenTopics === 0 ? (
                        <tr className={`border-t border-border/70 ${topicTone}`}>
                          <td className="border-l-4 border-primary/20 px-3 py-2 pl-12 text-sm text-muted-foreground" colSpan={5}>
                            <Button
                              type="button"
                              ghost
                              size="sm"
                              onClick={() => setShowAllTopics((current) => ({ ...current, [group.id]: false }))}
                            >
                              Hide inherited topics
                            </Button>
                          </td>
                        </tr>
                      ) : null}
                    </Fragment>
                  );
                })}
                </>)}
              </tbody>
            </table>
          </div>
        </div>

        <Button type="button" className="self-start uppercase" size="sm" disabled={saving} onClick={save}>
          <Save className="h-4 w-4" />
          {saving ? "Saving..." : "Save changes"}
        </Button>

        {settingsTarget ? (
          <div
            className="fixed inset-0 z-[100] flex items-center justify-center bg-background/85 p-4 backdrop-blur-sm"
            role="dialog"
            aria-modal="true"
            aria-labelledby="chat-settings-title"
            onClick={(event) => {
              if (event.target === event.currentTarget) setSettingsTarget(null);
            }}
          >
            <div className="max-h-[90vh] w-full max-w-2xl overflow-y-auto border border-border bg-card shadow-2xl">
              <div className="border-b border-border p-4">
                <H2 id="chat-settings-title" variant="sm" className="text-muted-foreground">
                  {settingsTarget.kind === "defaults" ? "Default Chat Settings" : "Chat Settings"}
                </H2>
                {settingsTarget.kind === "chat" ? (
                  <>
                    <div className="mt-1 text-sm">{settingsTarget.group.chatLabel}</div>
                    <div className="font-mono text-xs text-muted-foreground">{settingsTarget.group.platform}:{settingsTarget.group.chat_id}</div>
                  </>
                ) : (
                  <div className="mt-1 text-sm text-muted-foreground">Applied to every chat unless the chat overrides a field.</div>
                )}
              </div>
              <div className="grid gap-4 p-4 md:grid-cols-2">
                {settingsTarget.kind === "defaults"
                  ? renderChatSettingsFields(chatDefaults, updateDefaultChatSetting)
                  : renderChatSettingsFields(
                    currentChatSetting(settingsTarget.group),
                    (patch) => updateChatSetting(settingsTarget.group, patch),
                  )}

                <div className="flex justify-end gap-2 md:col-span-2">
                  <Button type="button" ghost size="sm" onClick={() => setSettingsTarget(null)}>
                    Close
                  </Button>
                </div>
              </div>
            </div>
          </div>
        ) : null}
      </CardContent>
    </Card>
  );
}
