import { useEffect, useLayoutEffect, useRef, useState, useMemo } from "react";
import {
  Code,
  Download,
  FormInput,
  RotateCcw,
  Search,
  Upload,
  X,
  Settings2,
  FileText,
  Settings,
  Bot,
  Monitor,
  Palette,
  Users,
  Brain,
  Package,
  Lock,
  Globe,
  Mic,
  Volume2,
  Ear,
  ClipboardList,
  MessageCircle,
  Wrench,
  FileQuestion,
  Filter,
  Cloud,
  Sparkles,
  LayoutDashboard,
  BookOpen,
  Route,
  History,
  Shield,
  FileOutput,
  RefreshCw,
  Save,
} from "lucide-react";
import { api, HERMES_BASE_PATH } from "@/lib/api";
import type {
  GeminiTtsVoiceInfo,
  GeminiTtsVoicesResponse,
} from "@/lib/api";
import { getNestedValue, setNestedValue } from "@/lib/nested";
import { useToast } from "@nous-research/ui/hooks/use-toast";
import { Toast } from "@nous-research/ui/ui/components/toast";
import { AutoField } from "@/components/AutoField";
import { Button } from "@nous-research/ui/ui/components/button";
import { ListItem } from "@nous-research/ui/ui/components/list-item";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { Card, CardContent, CardHeader, CardTitle } from "@nous-research/ui/ui/components/card";
import { ConfirmDialog } from "@nous-research/ui/ui/components/confirm-dialog";
import { Input } from "@nous-research/ui/ui/components/input";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Label } from "@nous-research/ui/ui/components/label";
import { Select, SelectOption } from "@nous-research/ui/ui/components/select";
import { Switch } from "@nous-research/ui/ui/components/switch";
import { useI18n } from "@/i18n";
import { usePageHeader } from "@/contexts/usePageHeader";
import { PluginSlot } from "@/plugins";

/* ------------------------------------------------------------------ */
/*  Helpers                                                            */
/* ------------------------------------------------------------------ */

const CATEGORY_ICONS: Record<
  string,
  React.ComponentType<{ className?: string }>
> = {
  general: Settings,
  agent: Bot,
  terminal: Monitor,
  display: Palette,
  delegation: Users,
  memory: Brain,
  compression: Package,
  security: Lock,
  browser: Globe,
  voice: Mic,
  tts: Volume2,
  stt: Ear,
  logging: ClipboardList,
  discord: MessageCircle,
  auxiliary: Wrench,
  bedrock: Cloud,
  curator: Sparkles,
  kanban: LayoutDashboard,
  model_catalog: BookOpen,
  openrouter: Route,
  sessions: History,
  tool_loop_guardrails: Shield,
  tool_output: FileOutput,
  updates: RefreshCw,
};

function CategoryIcon({
  category,
  className,
}: {
  category: string;
  className?: string;
}) {
  const Icon = CATEGORY_ICONS[category] ?? FileQuestion;
  return <Icon className={className ?? "h-4 w-4"} />;
}

type TtsDraft = {
  provider: string;
  voice: string;
  model: string;
  fallback_model: string;
  style: string;
};

const TTS_PROVIDER_OPTIONS = [
  "gemini",
  "edge",
  "elevenlabs",
  "openai",
  "xai",
  "piper",
  "neutts",
];

const DEFAULT_TTS_DRAFT: TtsDraft = {
  provider: "gemini",
  voice: "Enceladus",
  model: "gemini-3.1-flash-tts-preview",
  fallback_model: "gemini-2.5-flash-preview-tts",
  style: "",
};

function draftFromGeminiInfo(info: GeminiTtsVoicesResponse): TtsDraft {
  return {
    provider: info.provider || "gemini",
    voice: info.voice || info.reference?.voice || "Enceladus",
    model: info.model || info.reference?.model || "gemini-3.1-flash-tts-preview",
    fallback_model: info.fallback_model || "gemini-2.5-flash-preview-tts",
    style: info.style || info.reference?.style || "",
  };
}

function voiceSampleLabel(voice: GeminiTtsVoiceInfo): string {
  const kind =
    voice.sample_kind === "m"
      ? "male"
      : voice.sample_kind === "w"
        ? "female"
        : voice.sample_kind === "x"
          ? "neutral"
          : "";
  return kind ? `${voice.name} (${kind})` : voice.name;
}

/* ------------------------------------------------------------------ */
/*  Component                                                          */
/* ------------------------------------------------------------------ */

export default function ConfigPage() {
  const [config, setConfig] = useState<Record<string, unknown> | null>(null);
  const [schema, setSchema] = useState<Record<
    string,
    Record<string, unknown>
  > | null>(null);
  const [categoryOrder, setCategoryOrder] = useState<string[]>([]);
  const [defaults, setDefaults] = useState<Record<string, unknown> | null>(
    null,
  );
  const [saving, setSaving] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");
  const [yamlMode, setYamlMode] = useState(false);
  const [yamlText, setYamlText] = useState("");
  const [yamlLoading, setYamlLoading] = useState(false);
  const [yamlSaving, setYamlSaving] = useState(false);
  const [configPath, setConfigPath] = useState<string | null>(null);
  const [activeCategory, setActiveCategory] = useState<string>("");
  const [confirmReset, setConfirmReset] = useState(false);
  const [geminiTtsInfo, setGeminiTtsInfo] =
    useState<GeminiTtsVoicesResponse | null>(null);
  const [ttsDraft, setTtsDraft] = useState<TtsDraft>(DEFAULT_TTS_DRAFT);
  const [ttsLoading, setTtsLoading] = useState(false);
  const [ttsSaving, setTtsSaving] = useState(false);
  const [ttsApplyProfiles, setTtsApplyProfiles] = useState(true);
  const { toast, showToast } = useToast();
  const fileInputRef = useRef<HTMLInputElement>(null);
  const { t } = useI18n();
  const { setEnd } = usePageHeader();

  useLayoutEffect(() => {
    if (!config || !schema) {
      setEnd(null);
      return;
    }
    setEnd(
      <div className="relative w-full min-w-0 sm:max-w-xs">
        <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-muted-foreground" />
        <Input
          className="h-8 pl-8 pr-7 text-xs"
          placeholder={t.common.search}
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
        />
        {searchQuery && (
          <Button
            ghost
            size="xs"
            className="absolute right-1.5 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
            onClick={() => setSearchQuery("")}
            aria-label={t.common.clear}
          >
            <X />
          </Button>
        )}
      </div>,
    );
    return () => setEnd(null);
  }, [config, schema, searchQuery, setEnd, t.common.clear, t.common.search]);

  function prettyCategoryName(cat: string): string {
    const key = cat as keyof typeof t.config.categories;
    if (t.config.categories[key]) return t.config.categories[key];
    return cat.charAt(0).toUpperCase() + cat.slice(1);
  }

  useEffect(() => {
    api
      .getConfig()
      .then(setConfig)
      .catch(() => {});
    api
      .getSchema()
      .then((resp) => {
        setSchema(resp.fields as Record<string, Record<string, unknown>>);
        setCategoryOrder(resp.category_order ?? []);
      })
      .catch(() => {});
    api
      .getDefaults()
      .then(setDefaults)
      .catch(() => {});
    api
      .getStatus()
      .then((resp) => setConfigPath(resp.config_path))
      .catch(() => {});
  }, []);

  useEffect(() => {
    setTtsLoading(true);
    api
      .getGeminiTtsVoices()
      .then((resp) => {
        setGeminiTtsInfo(resp);
        setTtsDraft(draftFromGeminiInfo(resp));
      })
      .catch(() => showToast("Failed to load Gemini voices", "error"))
      .finally(() => setTtsLoading(false));
  }, []);

  // Set active category when categories load
  useEffect(() => {
    if (categoryOrder.length > 0 && !activeCategory) {
      setActiveCategory(categoryOrder[0]);
    }
  }, [categoryOrder, activeCategory]);

  // Load YAML when switching to YAML mode
  useEffect(() => {
    if (yamlMode) {
      setYamlLoading(true);
      api
        .getConfigRaw()
        .then((resp) => setYamlText(resp.yaml))
        .catch(() => showToast(t.config.failedToLoadRaw, "error"))
        .finally(() => setYamlLoading(false));
    }
  }, [yamlMode]);

  /* ---- Categories ---- */
  const categories = useMemo(() => {
    if (!schema) return [];
    const allCats = [
      ...new Set(
        Object.values(schema).map((s) => String(s.category ?? "general")),
      ),
    ];
    const ordered = categoryOrder.filter((c) => allCats.includes(c));
    const extra = allCats.filter((c) => !categoryOrder.includes(c)).sort();
    return [...ordered, ...extra];
  }, [schema, categoryOrder]);

  /* ---- Category field counts ---- */
  const categoryCounts = useMemo(() => {
    if (!schema) return {};
    const counts: Record<string, number> = {};
    for (const s of Object.values(schema)) {
      const cat = String(s.category ?? "general");
      counts[cat] = (counts[cat] || 0) + 1;
    }
    return counts;
  }, [schema]);

  /* ---- Search ---- */
  const isSearching = searchQuery.trim().length > 0;
  const lowerSearch = searchQuery.toLowerCase();

  const searchMatchedFields = useMemo(() => {
    if (!isSearching || !schema) return [];
    return Object.entries(schema).filter(([key, s]) => {
      const label = key.split(".").pop() ?? key;
      const humanLabel = label.replace(/_/g, " ");
      return (
        key.toLowerCase().includes(lowerSearch) ||
        humanLabel.toLowerCase().includes(lowerSearch) ||
        String(s.category ?? "")
          .toLowerCase()
          .includes(lowerSearch) ||
        String(s.description ?? "")
          .toLowerCase()
          .includes(lowerSearch)
      );
    });
  }, [isSearching, lowerSearch, schema]);

  /* ---- Active tab fields ---- */
  const activeFields = useMemo(() => {
    if (!schema || isSearching) return [];
    return Object.entries(schema).filter(
      ([, s]) => String(s.category ?? "general") === activeCategory,
    );
  }, [schema, activeCategory, isSearching]);

  /* ---- Handlers ---- */
  const handleSave = async () => {
    if (!config) return;
    setSaving(true);
    try {
      await api.saveConfig(config);
      showToast(t.config.configSaved, "success");
    } catch (e) {
      showToast(`${t.config.failedToSave}: ${e}`, "error");
    } finally {
      setSaving(false);
    }
  };

  const handleTtsPanelSave = async () => {
    if (!config) return;
    setTtsSaving(true);
    try {
      const resp = await api.saveGeminiTtsSettings({
        provider: ttsDraft.provider,
        voice: ttsDraft.voice,
        model: ttsDraft.model,
        fallback_model: ttsDraft.fallback_model,
        style: ttsDraft.style,
        apply_to_profiles: ttsApplyProfiles,
      });
      setGeminiTtsInfo(resp);
      setTtsDraft(draftFromGeminiInfo(resp));

      let next = setNestedValue(config, "tts.provider", resp.provider);
      next = setNestedValue(next, "tts.gemini.voice", resp.voice);
      next = setNestedValue(next, "tts.gemini.model", resp.model);
      next = setNestedValue(next, "tts.gemini.fallback_model", resp.fallback_model);
      next = setNestedValue(next, "tts.gemini.style", resp.style);
      setConfig(next);

      const updatedProfiles = resp.updated_profiles?.length ?? 0;
      showToast(
        updatedProfiles > 0
          ? `TTS saved; updated ${updatedProfiles} profile override${updatedProfiles === 1 ? "" : "s"}`
          : "TTS settings saved",
        "success",
      );
    } catch (e) {
      showToast(`Failed to save TTS settings: ${e}`, "error");
    } finally {
      setTtsSaving(false);
    }
  };

  const handleYamlSave = async () => {
    setYamlSaving(true);
    try {
      await api.saveConfigRaw(yamlText);
      showToast(t.config.yamlConfigSaved, "success");
      api
        .getConfig()
        .then(setConfig)
        .catch(() => {});
    } catch (e) {
      showToast(`${t.config.failedToSaveYaml}: ${e}`, "error");
    } finally {
      setYamlSaving(false);
    }
  };

  const handleReset = () => {
    if (!defaults || !config) return;
    // Scope the reset to what the user is currently looking at:
    //   - search mode → the matched fields
    //   - form mode   → the active category's fields
    // Resetting the whole config here was a footgun (issue reported by @ykmfb001):
    // the button sits next to the category tabs and users reasonably assumed
    // "reset this tab", not "wipe my entire config.yaml".
    const scopedFields = isSearching ? searchMatchedFields : activeFields;
    if (scopedFields.length === 0) return;
    setConfirmReset(true);
  };

  const executeReset = () => {
    if (!defaults || !config) return;
    setConfirmReset(false);
    const scopedFields = isSearching ? searchMatchedFields : activeFields;
    if (scopedFields.length === 0) return;
    const scopeLabel = isSearching
      ? t.config.searchResults
      : prettyCategoryName(activeCategory);
    let next: Record<string, unknown> = config;
    for (const [key] of scopedFields) {
      next = setNestedValue(next, key, getNestedValue(defaults, key));
    }
    setConfig(next);
    showToast(
      t.config.resetScopeToast.replace("{scope}", scopeLabel),
      "success",
    );
  };

  const handleExport = () => {
    if (!config) return;
    const blob = new Blob([JSON.stringify(config, null, 2)], {
      type: "application/json",
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "hermes-config.json";
    a.click();
    URL.revokeObjectURL(url);
  };

  const handleImport = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      try {
        const imported = JSON.parse(reader.result as string);
        setConfig(imported);
        showToast(t.config.configImported, "success");
      } catch {
        showToast(t.config.invalidJson, "error");
      }
    };
    reader.readAsText(file);
  };

  /* ---- Loading ---- */
  if (!config || !schema) {
    return (
      <div className="flex items-center justify-center py-24">
        <Spinner className="text-2xl text-primary" />
      </div>
    );
  }

  /* ---- Render field list (shared between search & normal) ---- */
  const renderFields = (
    fields: [string, Record<string, unknown>][],
    showCategory = false,
  ) => {
    let lastSection = "";
    let lastCat = "";
    return fields.map(([key, s]) => {
      const parts = key.split(".");
      const section = parts.length > 1 ? parts[0] : "";
      const cat = String(s.category ?? "general");
      const showCatBadge = showCategory && cat !== lastCat;
      const showSection =
        !showCategory &&
        section &&
        section !== lastSection &&
        section !== activeCategory;
      lastSection = section;
      lastCat = cat;

      return (
        <div key={key}>
          {showCatBadge && (
            <div className="flex items-center gap-2 pt-4 pb-2 first:pt-0">
              <CategoryIcon
                category={cat}
                className="h-4 w-4 text-muted-foreground"
              />
              <span className="font-mondwest text-display text-xs font-semibold tracking-wider text-muted-foreground">
                {prettyCategoryName(cat)}
              </span>
              <div className="flex-1 border-t border-border" />
            </div>
          )}
          {showSection && (
            <div className="flex items-center gap-2 pt-4 pb-2 first:pt-0">
              <span className="font-mondwest text-display text-xs font-semibold tracking-wider text-muted-foreground">
                {section.replace(/_/g, " ")}
              </span>
              <div className="flex-1 border-t border-border" />
            </div>
          )}
          <div className="py-1">
            <AutoField
              schemaKey={key}
              schema={s}
              value={getNestedValue(config, key)}
              onChange={(v) => setConfig(setNestedValue(config, key, v))}
            />
          </div>
        </div>
      );
    });
  };

  const selectedGeminiVoice = geminiTtsInfo?.voices.find(
    (voice) => voice.name === ttsDraft.voice,
  );
  const selectedSampleSrc = selectedGeminiVoice?.sample_url
    ? `${HERMES_BASE_PATH}${selectedGeminiVoice.sample_url}`
    : "";
  const profileOverrideCount =
    geminiTtsInfo?.profiles.filter((profile) => profile.has_gemini_override)
      .length ?? 0;
  const nanoclawVoice = geminiTtsInfo?.reference?.voice || "Enceladus";
  const panelCanSave =
    ttsDraft.provider.trim().length > 0 &&
    (ttsDraft.provider !== "gemini" || ttsDraft.voice.trim().length > 0) &&
    !ttsSaving;

  return (
    <div className="flex flex-col gap-4">
      <PluginSlot name="config:top" />
      <Toast toast={toast} />

      <div className="flex min-w-0 flex-col gap-3 sm:flex-row sm:items-center sm:justify-between sm:gap-4">
        <div className="flex min-w-0 items-center gap-2 sm:flex-1">
          <Settings2 className="h-4 w-4 shrink-0 text-muted-foreground" />
          <code className="min-w-0 flex-1 break-words text-xs text-muted-foreground bg-muted/50 px-2 py-0.5">
            {configPath ?? t.config.configPath}
          </code>
        </div>
        <div className="flex flex-wrap items-center gap-1.5 sm:shrink-0">
          <Button
            ghost
            size="icon"
            onClick={handleExport}
            title={t.config.exportConfig}
            aria-label={t.config.exportConfig}
          >
            <Download />
          </Button>
          <Button
            ghost
            size="icon"
            onClick={() => fileInputRef.current?.click()}
            title={t.config.importConfig}
            aria-label={t.config.importConfig}
          >
            <Upload />
          </Button>
          <input
            ref={fileInputRef}
            type="file"
            accept=".json"
            className="hidden"
            onChange={handleImport}
          />
          {!yamlMode &&
            (() => {
              const resetScopeLabel = isSearching
                ? t.config.searchResults
                : prettyCategoryName(activeCategory);
              const resetTitle = t.config.resetScopeTooltip.replace(
                "{scope}",
                resetScopeLabel,
              );
              return (
                <Button
                  ghost
                  size="icon"
                  onClick={handleReset}
                  title={resetTitle}
                  aria-label={resetTitle}
                >
                  <RotateCcw />
                </Button>
              );
            })()}

          <div className="w-px h-5 bg-border mx-1" />

          <Button
            size="sm"
            outlined={!yamlMode}
            onClick={() => setYamlMode(!yamlMode)}
            prefix={yamlMode ? <FormInput /> : <Code />}
          >
            {yamlMode ? t.common.form : "YAML"}
          </Button>

          {yamlMode ? (
            <Button
              size="sm"
              className="uppercase"
              onClick={handleYamlSave}
              disabled={yamlSaving}
            >
              {yamlSaving ? t.common.saving : t.common.save}
            </Button>
          ) : (
            <Button
              size="sm"
              className="uppercase"
              onClick={handleSave}
              disabled={saving}
            >
              {saving ? t.common.saving : t.common.save}
            </Button>
          )}
        </div>
      </div>

      {yamlMode ? (
        <Card>
          <CardHeader className="py-3 px-4">
            <CardTitle className="text-sm flex items-center gap-2">
              <FileText className="h-4 w-4" />
              {t.config.rawYaml}
            </CardTitle>
          </CardHeader>
          <CardContent className="p-0">
            {yamlLoading ? (
              <div className="flex items-center justify-center py-12">
                <Spinner className="text-xl text-primary" />
              </div>
            ) : (
              <textarea
                className="flex min-h-[600px] w-full bg-transparent px-4 py-3 text-sm font-mono leading-relaxed placeholder:text-muted-foreground focus-visible:outline-none border-t border-border"
                value={yamlText}
                onChange={(e) => setYamlText(e.target.value)}
                spellCheck={false}
              />
            )}
          </CardContent>
        </Card>
      ) : (
        <div className="flex flex-col sm:flex-row gap-4">
          <aside aria-label={t.config.filters} className="sm:w-56 sm:shrink-0">
            <div className="sm:sticky sm:top-4">
              <div className="flex flex-col border border-border bg-muted/20">
                <div className="hidden sm:flex items-center gap-2 px-3 py-2 border-b border-border">
                  <Filter className="h-3 w-3 text-text-tertiary" />
                  <span className="font-mondwest text-display text-xs tracking-[0.12em] text-text-secondary">
                    {t.config.filters}
                  </span>
                </div>

                <div className="hidden sm:block px-3 pt-2 pb-1 font-mondwest text-display text-xs tracking-[0.12em] text-text-tertiary">
                  {t.config.sections}
                </div>

                <div className="flex sm:flex-col gap-1 sm:gap-px p-2 sm:pt-1 overflow-x-auto sm:overflow-x-visible scrollbar-none sm:max-h-[calc(100vh-260px)] sm:overflow-y-auto">
                  {categories.map((cat) => {
                    const isActive = !isSearching && activeCategory === cat;

                    return (
                      <ListItem
                        key={cat}
                        active={isActive}
                        onClick={() => {
                          setSearchQuery("");
                          setActiveCategory(cat);
                        }}
                        className="rounded-none whitespace-nowrap px-2 py-1 text-xs"
                      >
                        <CategoryIcon
                          category={cat}
                          className="h-3.5 w-3.5 shrink-0"
                        />
                        <span className="flex-1 truncate">
                          {prettyCategoryName(cat)}
                        </span>
                        <span
                          className={`text-xs tabular-nums ${
                            isActive
                              ? "text-text-secondary"
                              : "text-text-tertiary"
                          }`}
                        >
                          {categoryCounts[cat] || 0}
                        </span>
                      </ListItem>
                    );
                  })}
                </div>
              </div>
            </div>
          </aside>

          <div className="flex flex-1 min-w-0 flex-col gap-4">
            {!isSearching && activeCategory === "tts" && (
              <Card>
                <CardHeader className="py-3 px-4">
                  <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
                    <CardTitle className="text-sm flex items-center gap-2">
                      <Volume2 className="h-4 w-4" />
                      Voice tuning
                    </CardTitle>
                    <div className="flex flex-wrap items-center gap-1.5">
                      <Badge tone="secondary" className="text-xs">
                        NanoClaw: {nanoclawVoice}
                      </Badge>
                      <Badge tone="outline" className="text-xs">
                        {profileOverrideCount} profile override
                        {profileOverrideCount === 1 ? "" : "s"}
                      </Badge>
                    </div>
                  </div>
                </CardHeader>
                <CardContent className="grid gap-4 px-4 pb-4">
                  {ttsLoading ? (
                    <div className="flex items-center justify-center py-8">
                      <Spinner className="text-xl text-primary" />
                    </div>
                  ) : (
                    <>
                      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
                        <div className="grid min-w-0 gap-1.5">
                          <Label htmlFor="tts-provider" className="text-xs">
                            Provider
                          </Label>
                          <Select
                            id="tts-provider"
                            value={ttsDraft.provider}
                            onValueChange={(value) =>
                              setTtsDraft((draft) => ({
                                ...draft,
                                provider: value,
                              }))
                            }
                          >
                            {TTS_PROVIDER_OPTIONS.map((provider) => (
                              <SelectOption key={provider} value={provider}>
                                {provider}
                              </SelectOption>
                            ))}
                          </Select>
                        </div>

                        <div className="grid min-w-0 gap-1.5">
                          <Label htmlFor="tts-gemini-voice" className="text-xs">
                            Gemini voice
                          </Label>
                          <Select
                            id="tts-gemini-voice"
                            disabled={ttsDraft.provider !== "gemini"}
                            value={ttsDraft.voice}
                            onValueChange={(value) =>
                              setTtsDraft((draft) => ({
                                ...draft,
                                voice: value,
                              }))
                            }
                          >
                            {(geminiTtsInfo?.voices ?? []).map((voice) => (
                              <SelectOption key={voice.name} value={voice.name}>
                                {voiceSampleLabel(voice)}
                              </SelectOption>
                            ))}
                          </Select>
                        </div>

                        <div className="grid min-w-0 gap-1.5">
                          <Label htmlFor="tts-gemini-model" className="text-xs">
                            Model
                          </Label>
                          <Input
                            id="tts-gemini-model"
                            value={ttsDraft.model}
                            onChange={(e) =>
                              setTtsDraft((draft) => ({
                                ...draft,
                                model: e.target.value,
                              }))
                            }
                          />
                        </div>

                        <div className="grid min-w-0 gap-1.5">
                          <Label
                            htmlFor="tts-gemini-fallback-model"
                            className="text-xs"
                          >
                            Fallback
                          </Label>
                          <Input
                            id="tts-gemini-fallback-model"
                            value={ttsDraft.fallback_model}
                            onChange={(e) =>
                              setTtsDraft((draft) => ({
                                ...draft,
                                fallback_model: e.target.value,
                              }))
                            }
                          />
                        </div>
                      </div>

                      <div className="grid gap-3 lg:grid-cols-[minmax(0,1fr)_minmax(280px,360px)]">
                        <div className="grid min-w-0 gap-1.5">
                          <Label htmlFor="tts-gemini-style" className="text-xs">
                            Style
                          </Label>
                          <textarea
                            id="tts-gemini-style"
                            className="flex min-h-[116px] w-full border border-border bg-background/40 px-3 py-2 text-sm leading-relaxed placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-foreground/30 focus-visible:border-foreground/25"
                            value={ttsDraft.style}
                            onChange={(e) =>
                              setTtsDraft((draft) => ({
                                ...draft,
                                style: e.target.value,
                              }))
                            }
                          />
                        </div>

                        <div className="grid min-w-0 content-start gap-3">
                          <div className="grid min-w-0 gap-1.5">
                            <Label className="text-xs">
                              Sample
                            </Label>
                            {selectedSampleSrc ? (
                              <audio
                                key={selectedSampleSrc}
                                controls
                                className="h-9 w-full"
                                src={selectedSampleSrc}
                              />
                            ) : (
                              <div className="flex h-9 items-center border border-border px-3 text-xs text-muted-foreground">
                                No local sample
                              </div>
                            )}
                          </div>

                          <div className="flex items-center justify-between gap-3 border border-border bg-muted/20 px-3 py-2">
                            <Label
                              htmlFor="tts-apply-profiles"
                              className="text-xs cursor-pointer"
                            >
                              Apply profile overrides
                            </Label>
                            <Switch
                              id="tts-apply-profiles"
                              checked={ttsApplyProfiles}
                              onCheckedChange={setTtsApplyProfiles}
                            />
                          </div>

                          <div className="flex justify-end">
                            <Button
                              size="sm"
                              className="uppercase"
                              onClick={handleTtsPanelSave}
                              disabled={!panelCanSave}
                              prefix={ttsSaving ? <Spinner /> : <Save />}
                            >
                              {ttsSaving ? t.common.saving : t.common.save}
                            </Button>
                          </div>
                        </div>
                      </div>
                    </>
                  )}
                </CardContent>
              </Card>
            )}

            {isSearching ? (
              <Card>
                <CardHeader className="py-3 px-4">
                  <div className="flex items-center justify-between">
                    <CardTitle className="text-sm flex items-center gap-2">
                      <Search className="h-4 w-4" />
                      {t.config.searchResults}
                    </CardTitle>
                    <Badge tone="secondary" className="text-xs">
                      {searchMatchedFields.length}{" "}
                      {t.config.fields.replace(
                        "{s}",
                        searchMatchedFields.length !== 1 ? "s" : "",
                      )}
                    </Badge>
                  </div>
                </CardHeader>
                <CardContent className="grid gap-2 px-4 pb-4">
                  {searchMatchedFields.length === 0 ? (
                    <p className="text-sm text-muted-foreground text-center py-8">
                      {t.config.noFieldsMatch.replace("{query}", searchQuery)}
                    </p>
                  ) : (
                    renderFields(searchMatchedFields, true)
                  )}
                </CardContent>
              </Card>
            ) : (
              /* Active category */
              <Card>
                <CardHeader className="py-3 px-4">
                  <div className="flex items-center justify-between">
                    <CardTitle className="text-sm flex items-center gap-2">
                      <CategoryIcon
                        category={activeCategory}
                        className="h-4 w-4"
                      />
                      {prettyCategoryName(activeCategory)}
                    </CardTitle>
                    <Badge tone="secondary" className="text-xs">
                      {activeFields.length}{" "}
                      {t.config.fields.replace(
                        "{s}",
                        activeFields.length !== 1 ? "s" : "",
                      )}
                    </Badge>
                  </div>
                </CardHeader>
                <CardContent className="grid gap-2 px-4 pb-4">
                  {renderFields(activeFields)}
                </CardContent>
              </Card>
            )}
          </div>
        </div>
      )}
      <PluginSlot name="config:bottom" />
      <ConfirmDialog
        open={confirmReset}
        onCancel={() => setConfirmReset(false)}
        onConfirm={executeReset}
        title={t.config.confirmResetScope.replace(
          "{scope}",
          isSearching
            ? t.config.searchResults
            : prettyCategoryName(activeCategory),
        )}
        description={`This will reset ${
          (isSearching ? searchMatchedFields : activeFields).length
        } field(s) to their default values.`}
        destructive
        confirmLabel={t.config.resetDefaults}
      />
    </div>
  );
}
