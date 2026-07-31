import { Fragment, useCallback, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { createPortal } from "react-dom";
import {
  ChevronDown,
  ChevronRight,
  CircleQuestionMark,
  Plus,
  RefreshCw,
  Save,
  Settings,
  Trash2,
  X,
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
  ProfileResolvedDisplaySettings,
  ProfileRoute,
  ProfileRoutesResponse,
  ProfileScope,
  ProfileScopesResponse,
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
  "audio_trigger",
  "show_transcription",
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
  audio_trigger: "default",
  show_transcription: "default",
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
  response_mode: "Ответы",
  audio_trigger: "Аудио-триггер",
  show_transcription: "Транскрипт",
  reply_to_mode: "Треды",
  tool_progress: "Инструменты",
  show_reasoning: "Reasoning",
  tool_preview_length: "Превью",
  interim_assistant_messages: "Реплики",
  long_running_notifications: "Долгие задачи",
  busy_ack_detail: "Занят",
  cleanup_progress: "Очистка",
  streaming: "Стриминг",
  gateway_restart_notification: "Сервис",
};

const CHAT_SETTING_VALUE_LABELS: Record<string, string> = {
  all: "все",
  mentions: "упоминания",
  on: "вкл",
  off: "выкл",
  first: "первый",
  new: "новые",
  verbose: "подробно",
};

type ChatSettingHelp = {
  summary: string;
  difference?: string;
  examples?: string[];
  values?: string[];
  note?: string;
};

type ChatSettingHelpPopup = {
  key: string;
  label: string;
  help: ChatSettingHelp;
  left: number;
  top: number;
  width: number;
};

function ChatSettingHelpBody({ help }: { help: ChatSettingHelp }) {
  return (
    <div className="grid gap-2 text-left">
      <p>{help.summary}</p>
      {help.difference ? (
        <p>
          <span className="font-medium text-foreground">Отличие: </span>
          {help.difference}
        </p>
      ) : null}
      {help.examples?.length ? (
        <div className="grid gap-1">
          <div className="font-medium text-foreground">Примеры</div>
          <ul className="grid gap-1">
            {help.examples.map((example) => (
              <li key={example} className="pl-3 [text-indent:-0.75rem]">- {example}</li>
            ))}
          </ul>
        </div>
      ) : null}
      {help.values?.length ? (
        <div className="grid gap-1">
          <div className="font-medium text-foreground">Значения</div>
          <ul className="grid gap-1">
            {help.values.map((value) => (
              <li key={value} className="pl-3 [text-indent:-0.75rem]">- {value}</li>
            ))}
          </ul>
        </div>
      ) : null}
      {help.note ? <p className="text-warning">{help.note}</p> : null}
    </div>
  );
}

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

type BooleanChatSettingField =
  | "audio_trigger"
  | "show_transcription"
  | "show_reasoning"
  | "interim_assistant_messages"
  | "long_running_notifications"
  | "busy_ack_detail"
  | "cleanup_progress"
  | "streaming"
  | "gateway_restart_notification";

type ProfileScopeMap = Record<string, ProfileScopesResponse>;

function saveSnapshot(
  data: ProfileRoutesResponse,
  chatDefaults: ProfileChatSetting,
  chatSettings: ProfileChatSetting[],
  profileScopesByProfile: ProfileScopeMap,
): string {
  return JSON.stringify({ data, chatDefaults, chatSettings, profileScopesByProfile });
}

function emptyProfileScopes(): ProfileScopesResponse {
  return { default_scope: "default", scopes: [] };
}

function profileScopeKey(platform: string, chatId: string, threadId: string): string {
  return `${platform}:${chatId}:${threadId || ""}`;
}

function sameScopeAssignment(
  scope: ProfileScope,
  item: Pick<DiscoveredProfileRoute, "platform" | "chat_id" | "thread_id">,
): boolean {
  return (
    scope.platform === item.platform &&
    scope.chat_id === item.chat_id &&
    (scope.thread_id || "") === (item.thread_id || "")
  );
}

function scopeIdForTopic(item: DiscoveredProfileRoute): string {
  return profileScopeKey(item.platform, item.chat_id, item.thread_id || "topic")
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 64) || `topic-${item.thread_id || Date.now()}`;
}

function scopeIdForChat(group: ChatTopicGroup): string {
  return profileScopeKey(group.platform, group.chat_id, "chat-context")
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 64) || `chat-context-${Date.now()}`;
}

function scopeNameForTopic(item: DiscoveredProfileRoute, topicName: string): string {
  const base = `topic-${topicName || item.thread_id || "scope"}`;
  const cleaned = base
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, "-")
    .replace(/-+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 64);
  if (!cleaned || !/^[a-z0-9]/.test(cleaned)) return `topic-${item.thread_id || "scope"}`.slice(0, 64);
  return cleaned;
}

async function fetchProfileScopes(names: string[]): Promise<ProfileScopeMap> {
  const entries = await Promise.all(
    names.map(async (name) => [name, await api.getProfileScopes(name)] as const),
  );
  return Object.fromEntries(entries);
}

export function ProfileRouteEditor({ profiles }: Props) {
  const [data, setData] = useState<ProfileRoutesResponse>({ default_profile: "default", routes: [] });
  const [discovered, setDiscovered] = useState<DiscoveredProfileRoute[]>([]);
  const [chatDefaults, setChatDefaults] = useState<ProfileChatSetting>(DEFAULT_CHAT_SETTINGS);
  const [chatSettings, setChatSettings] = useState<ProfileChatSetting[]>([]);
  const [profileScopesByProfile, setProfileScopesByProfile] = useState<ProfileScopeMap>({});
  const [expandedChats, setExpandedChats] = useState<Record<string, boolean>>({});
  const [showAllTopics, setShowAllTopics] = useState<Record<string, boolean>>({});
  const [settingsTarget, setSettingsTarget] = useState<SettingsTarget | null>(null);
  const [chatSettingHelpPopup, setChatSettingHelpPopup] = useState<ChatSettingHelpPopup | null>(null);
  const [resolvedDisplaySettings, setResolvedDisplaySettings] = useState<Record<string, ProfileResolvedDisplaySettings>>({});
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const [loadingKnown, setLoadingKnown] = useState(false);
  const [savedSnapshot, setSavedSnapshot] = useState("");

  const profileNames = useMemo(
    () => Array.from(new Set(["default", ...profiles.map((profile) => profile.name)])).filter(Boolean),
    [profiles],
  );
  const currentSnapshot = useMemo(
    () => saveSnapshot(data, chatDefaults, chatSettings, profileScopesByProfile),
    [data, chatDefaults, chatSettings, profileScopesByProfile],
  );
  const hasUnsavedChanges = savedSnapshot !== "" && currentSnapshot !== savedSnapshot;

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
    const [routes, known, settings, scopeMap] = await Promise.all([
      api.getProfileRoutes(),
      api.getDiscoveredProfileRoutes(),
      api.getProfileChatSettings(),
      fetchProfileScopes(profileNames),
    ]);
    const normalizedDefaults = { ...DEFAULT_CHAT_SETTINGS, ...(settings.defaults || {}) };
    setData(routes);
    setDiscovered(known.items);
    setChatDefaults(normalizedDefaults);
    setChatSettings(settings.settings);
    setProfileScopesByProfile(scopeMap);
    setSavedSnapshot(saveSnapshot(routes, normalizedDefaults, settings.settings, scopeMap));
  }, [profileNames]);

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

  function topicScope(item: DiscoveredProfileRoute, profile: string): ProfileScope | undefined {
    return (profileScopesByProfile[profile]?.scopes || []).find((scope) => sameScopeAssignment(scope, item));
  }

  function chatScope(group: ChatTopicGroup, profile: string): ProfileScope | undefined {
    return (profileScopesByProfile[profile]?.scopes || []).find((scope) => (
      scope.platform === group.platform
      && scope.chat_id === group.chat_id
      && !scope.thread_id
    ));
  }

  function chatTopicIsolationEnabled(group: ChatTopicGroup, profile: string): boolean {
    return chatScope(group, profile)?.topic_isolation !== false;
  }

  function topicIsolationEnabled(
    item: DiscoveredProfileRoute,
    profile: string,
    group: ChatTopicGroup,
  ): boolean {
    const direct = topicScope(item, profile);
    if (direct) return direct.topic_isolation !== false;
    return chatTopicIsolationEnabled(group, profile);
  }

  function setChatTopicIsolation(group: ChatTopicGroup, enabled: boolean) {
    const profile = chatProfile(group);
    setProfileScopesByProfile((current) => {
      const profileScopes = current[profile] || emptyProfileScopes();
      const defaultScope = profileScopes.default_scope || "default";
      const existingIndex = profileScopes.scopes.findIndex((scope) => (
        scope.platform === group.platform
        && scope.chat_id === group.chat_id
        && !scope.thread_id
      ));
      const existing = existingIndex >= 0 ? profileScopes.scopes[existingIndex] : undefined;
      const scopes = profileScopes.scopes.filter((_, index) => index !== existingIndex);
      const chatContextScope: ProfileScope = {
        id: existing?.id || scopeIdForChat(group),
        enabled: true,
        platform: group.platform,
        chat_id: group.chat_id,
        scope: existing?.scope || defaultScope,
        memory_scope: existing?.memory_scope || existing?.scope || defaultScope,
        label: existing?.label || group.chatLabel,
        topic_isolation: enabled,
        ...(existing?.skill_sets ? { skill_sets: existing.skill_sets } : {}),
      };
      return {
        ...current,
        [profile]: { ...profileScopes, scopes: [...scopes, chatContextScope] },
      };
    });
  }

  function setTopicIsolation(item: DiscoveredProfileRoute, group: ChatTopicGroup, enabled: boolean) {
    const profile = topicProfile(item, group);
    const topicName = topicLabelForItem(item, group.chatLabel);
    setProfileScopesByProfile((current) => {
      const profileScopes = current[profile] || emptyProfileScopes();
      const defaultScope = profileScopes.default_scope || "default";
      const existingIndex = profileScopes.scopes.findIndex((scope) => sameScopeAssignment(scope, item));
      const existing = existingIndex >= 0 ? profileScopes.scopes[existingIndex] : undefined;
      const scopes = profileScopes.scopes.filter((_, index) => index !== existingIndex);

      if (!enabled) {
        const inherited = chatScope(group, profile);
        const inheritedScope: ProfileScope = {
          id: existing?.id || scopeIdForTopic(item),
          enabled: true,
          platform: item.platform,
          chat_id: item.chat_id,
          thread_id: item.thread_id || "",
          scope: inherited?.scope || defaultScope,
          memory_scope: inherited?.memory_scope || inherited?.scope || defaultScope,
          label: existing?.label || topicName,
          topic_isolation: false,
          ...(existing?.skill_sets ? { skill_sets: existing.skill_sets } : {}),
        };
        return { ...current, [profile]: { ...profileScopes, scopes: [...scopes, inheritedScope] } };
      }

      const generatedScope = scopeNameForTopic(item, topicName);
      const nextScopeName = existing?.topic_isolation === true && existing.scope ? existing.scope : generatedScope;
      const isolatedScope: ProfileScope = {
        id: existing?.id || scopeIdForTopic(item),
        enabled: true,
        platform: item.platform,
        chat_id: item.chat_id,
        thread_id: item.thread_id || "",
        scope: nextScopeName,
        memory_scope: existing?.topic_isolation === true && existing.memory_scope ? existing.memory_scope : nextScopeName,
        label: existing?.label || topicName,
        topic_isolation: true,
        ...(existing?.skill_sets ? { skill_sets: existing.skill_sets } : {}),
      };
      return { ...current, [profile]: { ...profileScopes, scopes: [...scopes, isolatedScope] } };
    });
  }

  function currentChatSetting(group: ChatTopicGroup): ProfileChatSetting {
    return chatSettingsByKey.get(group.id) || defaultChatSetting(group);
  }

  function resolvedDisplayKey(profile: string, platform: string, chatId = ""): string {
    return `${profile}:${platform}:${chatId}`;
  }

  function loadResolvedDisplaySettings(profile: string, platform: string, chatId = "") {
    const key = resolvedDisplayKey(profile, platform, chatId);
    if (resolvedDisplaySettings[key]) return;
    api.getProfileResolvedDisplaySettings(profile, platform, chatId)
      .then((result) => {
        setResolvedDisplaySettings((current) => ({ ...current, [key]: result.settings }));
      })
      .catch((err) => {
        setError(String((err as Error).message || err));
      });
  }

  function openSettings(target: SettingsTarget) {
    setChatSettingHelpPopup(null);
    setSettingsTarget(target);
    if (target.kind === "chat") {
      const profile = chatProfile(target.group);
      loadResolvedDisplaySettings(profile, target.group.platform, target.group.chat_id);
    } else {
      loadResolvedDisplaySettings(data.default_profile || "default", "telegram");
    }
  }

  function closeSettings() {
    setChatSettingHelpPopup(null);
    setSettingsTarget(null);
  }

  function toggleChatSettingHelp(
    key: string,
    label: string,
    help: ChatSettingHelp,
    anchor: HTMLElement,
  ) {
    setChatSettingHelpPopup((current) => {
      if (current?.key === key) return null;
      const rect = anchor.getBoundingClientRect();
      const viewportWidth = window.innerWidth || document.documentElement.clientWidth || 1024;
      const viewportHeight = window.innerHeight || document.documentElement.clientHeight || 768;
      const margin = 12;
      const width = Math.min(420, Math.max(280, viewportWidth - margin * 2));
      let left = rect.left + rect.width / 2 - width / 2;
      left = Math.max(margin, Math.min(left, viewportWidth - width - margin));

      const estimatedHeight = Math.min(360, viewportHeight - margin * 2);
      let top = rect.bottom + 8;
      if (top + estimatedHeight > viewportHeight - margin) {
        top = Math.max(margin, rect.top - estimatedHeight - 8);
      }
      return { key, label, help, left, top, width };
    });
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

  async function save(): Promise<boolean> {
    setSaving(true);
    setError("");
    try {
      await api.updateProfileRoutes(data);
      await api.updateProfileChatSettings({ defaults: chatDefaults, settings: chatSettings });
      await Promise.all(
        Object.entries(profileScopesByProfile).map(([name, scopes]) => api.updateProfileScopes(name, scopes)),
      );
      const [routes, known, settings, scopeMap] = await Promise.all([
        api.getProfileRoutes(),
        api.getDiscoveredProfileRoutes(),
        api.getProfileChatSettings(),
        fetchProfileScopes(profileNames),
      ]);
      const normalizedDefaults = { ...DEFAULT_CHAT_SETTINGS, ...(settings.defaults || {}) };
      setData(routes);
      setDiscovered(known.items);
      setChatDefaults(normalizedDefaults);
      setChatSettings(settings.settings);
      setProfileScopesByProfile(scopeMap);
      setSavedSnapshot(saveSnapshot(routes, normalizedDefaults, settings.settings, scopeMap));
      return true;
    } catch (err) {
      setError(String((err as Error).message || err));
      return false;
    } finally {
      setSaving(false);
    }
  }

  function renderChatSettingsFields(
    setting: ProfileChatSetting,
    onChange: (patch: Partial<ProfileChatSetting>) => void,
  ) {
    const selectClass = "h-9 w-full min-w-0 border border-input bg-transparent px-2 text-sm text-foreground";
    const activeProfile = settingsTarget?.kind === "chat"
      ? chatProfile(settingsTarget.group)
      : data.default_profile || "default";
    const activePlatform = settingsTarget?.kind === "chat" ? settingsTarget.group.platform : "telegram";
    const activeChatId = settingsTarget?.kind === "chat" ? settingsTarget.group.chat_id : "";
    const resolved = resolvedDisplaySettings[resolvedDisplayKey(activeProfile, activePlatform, activeChatId)];
    const helpScope = settingsTarget?.kind || "chat-settings";

    const valueLabel = (field: (typeof CHAT_SETTING_KEYS)[number], value: unknown): string => {
      if (value === undefined || value === null || value === "") return "ещё загружается";
      if (typeof value === "boolean") return value ? "Включить" : "Выключить";
      if (field === "tool_preview_length") {
        const numeric = Number(value);
        if (!Number.isFinite(numeric)) return String(value);
        return numeric === 0 ? "Стандартно" : `${numeric} символов`;
      }
      const normalized = String(value).trim().toLowerCase();
      const labelsByField: Partial<Record<(typeof CHAT_SETTING_KEYS)[number], Record<string, string>>> = {
        response_mode: {
          all: "Отвечать на все",
          mentions: "Только обращения",
        },
        reply_to_mode: {
          off: "Без reply-привязки",
          first: "Только первый фрагмент",
          all: "Все фрагменты",
        },
        tool_progress: {
          off: "Выключить",
          new: "Только новые инструменты",
          all: "Все запуски",
          verbose: "Подробно",
        },
      };
      const labels: Record<string, string> = {
        on: "Включить",
        off: "Выключить",
        ...(labelsByField[field] || {}),
      };
      return labels[normalized] || String(value);
    };

    const isInheritedValue = (value: unknown) => value === undefined || value === "" || value === "default";

    const inheritedValueLabel = (field: (typeof CHAT_SETTING_KEYS)[number]): string => {
      const chatDefault = chatDefaults[field];
      if (settingsTarget?.kind === "chat" && !isInheritedValue(chatDefault)) {
        return valueLabel(field, chatDefault);
      }
      const resolvedValue = resolved?.[field as keyof ProfileResolvedDisplaySettings];
      if (resolvedValue !== undefined) {
        return valueLabel(field, resolvedValue);
      }
      return "...";
    };

    const inheritOption = (field: (typeof CHAT_SETTING_KEYS)[number]) => (
      <option value="default">* {inheritedValueLabel(field)}</option>
    );

    const booleanFromValue = (value: unknown): boolean | null => {
      if (typeof value === "boolean") return value;
      const normalized = String(value ?? "").trim().toLowerCase();
      if (["on", "true", "1", "yes", "enabled"].includes(normalized)) return true;
      if (["off", "false", "0", "no", "disabled"].includes(normalized)) return false;
      return null;
    };

    const resolvedBooleanValue = (field: BooleanChatSettingField, value: unknown): boolean | null => {
      if (!isInheritedValue(value)) return booleanFromValue(value);
      const chatDefault = chatDefaults[field];
      if (settingsTarget?.kind === "chat" && !isInheritedValue(chatDefault)) {
        return booleanFromValue(chatDefault);
      }
      const resolvedValue = resolved?.[field as keyof ProfileResolvedDisplaySettings];
      return booleanFromValue(resolvedValue);
    };

    const booleanPatch = (
      field: BooleanChatSettingField,
      value: "default" | "on" | "off",
    ): Partial<ProfileChatSetting> => ({ [field]: value } as Partial<ProfileChatSetting>);

    const renderField = (
      field: (typeof CHAT_SETTING_KEYS)[number],
      label: string,
      help: ChatSettingHelp,
      control: ReactNode,
    ) => {
      const helpKey = `${helpScope}:${field}`;
      const helpOpen = chatSettingHelpPopup?.key === helpKey;
      return (
        <div className="relative grid gap-1.5 text-xs text-muted-foreground">
          <div className="flex items-center gap-1.5">
            <Label className="font-medium text-muted-foreground">{label}</Label>
            <button
              type="button"
              className="inline-flex h-5 w-5 items-center justify-center text-muted-foreground transition hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
              aria-label={`Справка: ${label}`}
              aria-expanded={helpOpen}
              title={`Справка: ${label}`}
              onClick={(event) => toggleChatSettingHelp(helpKey, label, help, event.currentTarget)}
            >
              <CircleQuestionMark className="h-3.5 w-3.5" />
            </button>
          </div>
        {control}
        </div>
      );
    };

    const renderBooleanField = (
      field: BooleanChatSettingField,
      label: string,
      help: ChatSettingHelp,
    ) => {
      const value = setting[field];
      const inherited = isInheritedValue(value);
      const resolvedValue = resolvedBooleanValue(field, value);
      const checked = resolvedValue === true;
      const checkboxState = resolvedValue === null ? "indeterminate" : checked;
      const helpKey = `${helpScope}:${field}`;
      const helpOpen = chatSettingHelpPopup?.key === helpKey;
      const controlId = `${helpKey}:checkbox`.replace(/[^a-zA-Z0-9_-]/g, "-");
      return (
        <div className="relative flex min-h-9 min-w-0 items-center gap-1.5 text-xs text-muted-foreground">
          <Checkbox
            id={controlId}
            checked={checkboxState}
            onCheckedChange={(next) => onChange(booleanPatch(field, next === true ? "on" : "off"))}
          />
          {inherited ? (
            <span className="shrink-0 text-muted-foreground" title="Наследуется">*</span>
          ) : null}
          <Label
            htmlFor={controlId}
            className={checked
              ? "min-w-0 flex-1 cursor-pointer text-sm leading-snug text-foreground"
              : "min-w-0 flex-1 cursor-pointer text-sm leading-snug text-muted-foreground"}
          >
            {label}
          </Label>
          <button
            type="button"
            className="inline-flex h-5 w-5 shrink-0 items-center justify-center text-muted-foreground transition hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
            aria-label={`Справка: ${label}`}
            aria-expanded={helpOpen}
            title={`Справка: ${label}`}
            onClick={(event) => toggleChatSettingHelp(helpKey, label, help, event.currentTarget)}
          >
            <CircleQuestionMark className="h-3.5 w-3.5" />
          </button>
          {!inherited ? (
            <button
              type="button"
              className="inline-flex h-5 w-5 shrink-0 items-center justify-center text-xs font-medium text-muted-foreground transition hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
              title="Наследовать"
              aria-label="Наследовать"
              onClick={() => onChange(booleanPatch(field, "default"))}
            >
              *
            </button>
          ) : null}
        </div>
      );
    };

    return (
      <>
        {renderField(
          "response_mode",
          "Когда отвечать",
          {
            summary: "Определяет, какие входящие сообщения вообще запускают бота в этом чате.",
            difference: "Это фильтр входящих сообщений. Он не меняет стиль ответа и не влияет на прогресс инструментов.",
            examples: [
              "«Отвечать на все сообщения»: бот реагирует на обычное «что по плану?» без упоминания.",
              "«Только обращения и команды»: бот молчит, пока его не упомянут, не ответят на его сообщение или не напишут команду.",
            ],
            values: [
              "*: взять значение из профиля или общих настроек чатов.",
              "Отвечать на все сообщения: режим личного помощника или маленького чата.",
              "Только обращения и команды: безопаснее для шумных групп.",
            ],
          },
          <select
            className={selectClass}
            value={setting.response_mode}
            onChange={(event) => onChange({ response_mode: event.target.value as ProfileChatSetting["response_mode"] })}
          >
            {inheritOption("response_mode")}
            <option value="all">Отвечать на все</option>
            <option value="mentions">Только обращения</option>
          </select>,
        )}

        {renderBooleanField(
          "audio_trigger",
          "Триггер в аудио",
          {
            summary: "Управляет тем, искать ли ключевые слова бота внутри голосовых и аудио, когда сообщение не обращено к боту текстом.",
            difference: "Это не публикует транскрипт само по себе. Если флаг включён, аудио распознаётся тихо только для проверки триггерных слов.",
            examples: [
              "Включить: голосовое с «трипио» или другим ключевым словом может запустить бота.",
              "Выключить: голосовое без текстового обращения не запускает бота по словам внутри аудио.",
              "Транскрипт в чат можно включить или выключить независимо от этого флага.",
            ],
            values: [
              "*: взять значение по умолчанию для чатов.",
              "Включить: слушать аудио на триггерные слова.",
              "Выключить: не запускаться от слов внутри аудио.",
            ],
          },
        )}

        {renderBooleanField(
          "show_transcription",
          "Транскрипт в чат",
          {
            summary: "Управляет только тем, отправлять ли уже распознанный текст голосового сообщения отдельным сообщением в этот чат.",
            difference: "Это не включает и не выключает аудио-триггер. Бот может публиковать транскрипт без запуска по ключевым словам или слушать триггеры без публикации текста.",
            examples: [
              "Включить: после голосового бот отправляет распознанный текст отдельным сообщением.",
              "Выключить: отдельное сообщение с транскриптом не отправляется.",
              "Аудио-триггер работает отдельно и не зависит от показа транскрипта в чат.",
            ],
            values: [
              "*: взять текущее значение из профиля или из telegram.show_transcription.",
              "Включить: публиковать распознанный текст в чат.",
              "Выключить: не публиковать распознанный текст в чат.",
            ],
          },
        )}

        {renderField(
          "reply_to_mode",
          "Привязка ответов",
          {
            summary: "Управляет Telegram reply-привязкой: будет ли сообщение бота визуально отвечать на исходное сообщение пользователя.",
            difference: "Это только оформление доставки в чате. Контекст разговора бот и так видит через историю.",
            examples: [
              "«Только первый фрагмент»: первый кусок ответа будет reply к твоему сообщению, остальные части пойдут обычными сообщениями.",
              "«Все фрагменты ответа»: каждый кусок длинного ответа будет reply к исходному сообщению.",
              "«Не привязывать»: бот ответит без Telegram-стрелки reply.",
            ],
            values: [
              "Выключить: чище лента, меньше reply-цепочек.",
              "Только первый фрагмент: обычно лучший баланс для длинных ответов.",
              "Все фрагменты: полезно в активных группах, где важно видеть, к чему относится каждый кусок.",
            ],
          },
          <select
            className={selectClass}
            value={setting.reply_to_mode}
            onChange={(event) => onChange({ reply_to_mode: event.target.value as ProfileChatSetting["reply_to_mode"] })}
          >
            {inheritOption("reply_to_mode")}
            <option value="off">Без reply-привязки</option>
            <option value="first">Только первый фрагмент</option>
            <option value="all">Все фрагменты</option>
          </select>,
        )}

        {renderField(
          "tool_progress",
          "Прогресс инструментов",
          {
            summary: "Показывает служебные статусы, когда бот запускает реальные инструменты: команды, поиск, чтение файлов, публикацию, обновление базы.",
            difference: "Это не мысли модели и не куски финального ответа. Это «что сейчас выполняется» на уровне инструментов.",
            examples: [
              "Статус: «exec_command: npm run build» или «web search: расписание».",
              "В твоем случае сюда относятся сообщения вроде «Need update DB, publish», если они приходят как tool status, а не как текст модели.",
            ],
            values: [
              "Выключен: не присылать статусы инструментов.",
              "Только новые инструменты: показывать смену инструмента и не спамить повтором одного и того же.",
              "Каждый запуск инструмента: показывать каждый tool call.",
              "Подробно, с аргументами: показывать больше технических деталей; лучше для отладки.",
            ],
            note: "Для обычного Telegram-чата обычно комфортнее «Только новые инструменты».",
          },
          <select
            className={selectClass}
            value={setting.tool_progress}
            onChange={(event) => onChange({ tool_progress: event.target.value as ProfileChatSetting["tool_progress"] })}
          >
            {inheritOption("tool_progress")}
            <option value="off">Выключить</option>
            <option value="new">Только новые инструменты</option>
            <option value="all">Все запуски</option>
            <option value="verbose">Подробно</option>
          </select>,
        )}

        {renderBooleanField(
          "show_reasoning",
          "Показывать reasoning",
          {
            summary: "Показывает отдельный reasoning-блок модели, если выбранная модель и провайдер вообще его отдают.",
            difference: "Это ближе всего к «мыслям», но это не прогресс инструментов и не обычные черновые фразы. Многие модели reasoning наружу не присылают.",
            examples: [
              "Может выглядеть как отдельный блок с ходом решения перед финальным ответом.",
              "Для бытового Telegram-чата обычно шумно и не нужно.",
            ],
            values: [
              "Включить: показывать reasoning, если он доступен.",
              "Выключить: скрывать reasoning и оставлять только нормальный ответ.",
            ],
            note: "Если цель — просто понимать, что бот не завис, включай «Прогресс инструментов», а не reasoning.",
          },
        )}

        {renderField(
          "tool_preview_length",
          "Длина превью аргументов",
          {
            summary: "Ограничивает размер текста, который показывается внутри статуса инструмента.",
            difference: "Работает только вместе с прогрессом инструментов. На финальный ответ, стриминг и reasoning не влияет.",
            examples: [
              "Если инструмент запускается с длинным запросом, превью можно обрезать до 80 символов.",
              "При подробном режиме это помогает не засорять чат большими JSON-аргументами.",
            ],
            values: [
              "Пусто: наследовать настройку.",
              "0: использовать стандартное поведение текущего режима.",
              "Например 80 или 120: явно ограничить длину превью.",
            ],
          },
          <Input
            type="number"
            min={0}
            placeholder={`* ${inheritedValueLabel("tool_preview_length")}`}
            value={setting.tool_preview_length === "default" ? "" : String(setting.tool_preview_length ?? "")}
            onChange={(event) => onChange({
              tool_preview_length: event.target.value === "" ? "default" : Number(event.target.value),
            })}
          />,
        )}

        {renderBooleanField(
          "interim_assistant_messages",
          "Промежуточные реплики модели",
          {
            summary: "Разрешает модели присылать короткие текстовые реплики по ходу работы до финального ответа.",
            difference: "Это именно текст модели, а не инструмент. Поэтому сюда могут просачиваться черновики вроде «Create new.» или «Publish mini app.»",
            examples: [
              "Нормальный вариант: «Сейчас проверю конфиг и вернусь с итогом».",
              "Плохой вариант: модель отправила внутреннюю заметку «Need update DB, publish.» как отдельное сообщение.",
            ],
            values: [
              "Включить: разрешить такие mid-turn сообщения.",
              "Выключить: ждать финальный ответ и статусы инструментов, если они включены.",
            ],
            note: "Для твоего бота я бы держал выключенным, чтобы не ловить англоязычные черновики.",
          },
        )}

        {renderBooleanField(
          "long_running_notifications",
          "Уведомления о долгих задачах",
          {
            summary: "Показывает отдельный сигнал, если задача идет долго и в чате иначе кажется, что бот завис.",
            difference: "Это не каждый tool call. Это редкое уведомление про задержку или долгий этап.",
            examples: [
              "«Работаю, это может занять еще немного времени» после долгого запуска инструмента.",
              "Полезно для публикаций, сборок, импорта данных, долгого поиска.",
            ],
            values: [
              "Включить: показывать такие ожидания.",
              "Выключить: молчать до финального ответа или обычного tool progress.",
            ],
          },
        )}

        {renderBooleanField(
          "busy_ack_detail",
          "Подробность ответа «занят»",
          {
            summary: "Управляет ответом бота, когда ты пишешь ему новую команду, а он еще занят предыдущей задачей.",
            difference: "Это не прогресс текущей задачи. Это реакция на новую входящую реплику во время занятости.",
            examples: [
              "Выключено: коротко «я занят».",
              "Включено: может объяснить, какая задача еще выполняется и что делать дальше.",
            ],
            values: [
              "Включить: больше деталей в ответе «занят».",
              "Выключить: короткий минимальный acknowledge.",
            ],
          },
        )}

        {renderBooleanField(
          "cleanup_progress",
          "Убирать временные статусы",
          {
            summary: "После финального ответа удаляет временные сообщения прогресса, если платформа умеет удалять такие сообщения.",
            difference: "Это уборка уже отправленных статусов. Она не включает и не выключает сам прогресс инструментов.",
            examples: [
              "Во время работы бот пишет «запускаю сборку», а после итогового ответа этот статус исчезает.",
              "Если задача упала с ошибкой, статусы могут остаться как диагностический след.",
            ],
            values: [
              "Включить: чат чище после успешного ответа.",
              "Выключить: оставить все статусы как историю работы.",
            ],
            note: "Работает только там, где adapter умеет удалять или редактировать сообщения.",
          },
        )}

        {renderBooleanField(
          "streaming",
          "Стриминг ответа",
          {
            summary: "Показывает финальный ответ по мере генерации, а не ждет, пока модель допишет весь текст.",
            difference: "Это куски обычного ответа, не мысли и не tool progress. На Telegram в личных чатах может обновляться draft/preview; на других платформах чаще редактируется одно сообщение или используется fallback к обычному финальному ответу.",
            examples: [
              "Без стриминга: бот молчит, потом присылает готовый длинный ответ.",
              "Со стримингом: ты видишь, как текст ответа постепенно появляется или обновляется.",
              "Если модель вызывает инструмент, стриминг текста может остановиться на время tool call; за это отвечает уже «Прогресс инструментов».",
            ],
            values: [
              "Включить: показывать ответ по частям, если платформа поддерживает.",
              "Выключить: присылать только готовый финальный ответ.",
            ],
            note: "Стриминг не должен озвучивать reasoning. Для «что бот делает сейчас» включай прогресс инструментов.",
          },
        )}

        {renderBooleanField(
          "gateway_restart_notification",
          "Сервисные уведомления gateway",
          {
            summary: "Сообщает в чат о сервисных событиях самого gateway: запуск, перезапуск, восстановление подключения.",
            difference: "Это инфраструктурные уведомления, не связанные с конкретным ответом модели.",
            examples: [
              "После рестарта процесса бот может написать, что gateway снова онлайн.",
              "Полезно для админского чата, обычно лишнее для обычной группы.",
            ],
            values: [
              "Включить: присылать сервисные события.",
              "Выключить: не шуметь техническими событиями gateway.",
            ],
          },
        )}
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
            onClick={() => openSettings({ kind: "defaults" })}
            title="Настройки по умолчанию для всех чатов"
          >
            <Settings className={settingHasOverrides(chatDefaults) ? "h-4 w-4 text-primary" : "h-4 w-4"} />
            Настройки чатов по умолчанию
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
                  const chatTopicsIsolated = chatTopicIsolationEnabled(group, profile);
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
                          {group.topics.length ? (
                            <>
                              <Label className="mt-2 flex items-center gap-2 text-xs text-muted-foreground">
                                <Checkbox
                                  checked={chatTopicsIsolated}
                                  onCheckedChange={(checked) => setChatTopicIsolation(group, checked === true)}
                                />
                                Topic isolation
                              </Label>
                              <div className="mt-1 text-xs text-muted-foreground">
                                {chatTopicsIsolated
                                  ? "Separate topic memory"
                                  : "Shared memory, parallel topic conversations"}
                              </div>
                            </>
                          ) : null}
                        </td>
                        <td className="px-3 py-2 text-right">
                          <Button
                            type="button"
                            ghost
                            size="icon"
                            aria-label={`Settings for ${group.chatLabel}`}
                            title={`Settings for ${group.chatLabel}`}
                            onClick={() => openSettings({ kind: "chat", group })}
                          >
                            <Settings className={settingHasOverrides(setting) ? "h-4 w-4 text-primary" : "h-4 w-4"} />
                          </Button>
                        </td>
                      </tr>

                      {expanded && visibleTopics.map((item) => {
                        const directProfile = topicDirectProfile(item);
                        const selected = directProfile && directProfile !== profile ? directProfile : INHERIT_PROFILE;
                        const topicName = topicLabelForItem(item, group.chatLabel);
                        const effectiveProfile = topicProfile(item, group);
                        const isolated = topicIsolationEnabled(item, effectiveProfile, group);
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
                              <div className="text-xs text-muted-foreground">{effectiveProfile}</div>
                              <Label className="mt-2 flex items-center gap-2 text-xs text-muted-foreground">
                                <Checkbox
                                  checked={isolated}
                                  onCheckedChange={(checked) => setTopicIsolation(item, group, checked === true)}
                                />
                                Topic isolation
                              </Label>
                              <div className="mt-1 text-xs text-muted-foreground">
                                {isolated ? "Own topic memory" : "Profile-level memory"}
                              </div>
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

        <div className="flex flex-wrap items-center gap-3">
          <Button type="button" className="uppercase" size="sm" disabled={saving} onClick={save}>
            <Save className="h-4 w-4" />
            {saving ? "Saving..." : "Save changes"}
          </Button>
          {savedSnapshot ? (
            <span className={hasUnsavedChanges ? "text-xs text-warning" : "text-xs text-muted-foreground"}>
              {saving ? "Сохраняю настройки..." : hasUnsavedChanges ? "Есть несохраненные изменения" : "Сохранено"}
            </span>
          ) : null}
        </div>

        {settingsTarget && typeof document !== "undefined" ? createPortal(
          <div
            className="fixed inset-0 z-[900] flex items-center justify-center bg-background/85 p-4 backdrop-blur-sm"
            role="dialog"
            aria-modal="true"
            aria-labelledby="chat-settings-title"
            onClick={(event) => {
              if (event.target === event.currentTarget) closeSettings();
            }}
          >
            <div className="max-h-[90vh] w-full max-w-4xl overflow-y-auto border border-border bg-card shadow-2xl">
              <div className="border-b border-border p-4">
                <H2 id="chat-settings-title" variant="sm" className="text-muted-foreground">
                  {settingsTarget.kind === "defaults" ? "Настройки чатов по умолчанию" : "Настройки чата"}
                </H2>
                {settingsTarget.kind === "chat" ? (
                  <>
                    <div className="mt-1 text-sm">{settingsTarget.group.chatLabel}</div>
                    <div className="font-mono text-xs text-muted-foreground">{settingsTarget.group.platform}:{settingsTarget.group.chat_id}</div>
                    <div className="mt-1 text-xs text-muted-foreground">Профиль чата: {chatProfile(settingsTarget.group)}</div>
                    <div className="mt-2 text-xs leading-relaxed text-muted-foreground">
                      Маркер * означает, что значение берётся по наследованию. Выбирай конкретное значение только если этот чат должен отличаться.
                    </div>
                  </>
                ) : (
                  <div className="mt-1 text-sm text-muted-foreground">
                    Применяется ко всем чатам, пока конкретный чат не переопределит отдельное поле.
                  </div>
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
                  <Button type="button" ghost size="sm" onClick={closeSettings}>
                    Закрыть
                  </Button>
                  <Button
                    type="button"
                    className="uppercase"
                    size="sm"
                    disabled={saving}
                    onClick={async () => {
                      const ok = await save();
                      if (ok) closeSettings();
                    }}
                  >
                    <Save className="h-4 w-4" />
                    {saving ? "Сохраняю..." : "Сохранить"}
                  </Button>
                </div>
              </div>
            </div>
          </div>,
          document.body,
        ) : null}
        {chatSettingHelpPopup && typeof document !== "undefined" ? createPortal(
          <div
            role="tooltip"
            className="fixed z-[1000] max-h-[min(70vh,360px)] overflow-y-auto border border-border bg-popover p-3 text-[11px] leading-relaxed text-popover-foreground shadow-2xl"
            style={{
              left: chatSettingHelpPopup.left,
              top: chatSettingHelpPopup.top,
              width: chatSettingHelpPopup.width,
            }}
          >
            <div className="mb-2 flex items-start justify-between gap-3">
              <div className="text-xs font-medium text-foreground">{chatSettingHelpPopup.label}</div>
              <button
                type="button"
                className="inline-flex h-5 w-5 shrink-0 items-center justify-center text-muted-foreground transition hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                aria-label="Закрыть справку"
                onClick={() => setChatSettingHelpPopup(null)}
              >
                <X className="h-3.5 w-3.5" />
              </button>
            </div>
            <ChatSettingHelpBody help={chatSettingHelpPopup.help} />
          </div>,
          document.body,
        ) : null}
      </CardContent>
    </Card>
  );
}
