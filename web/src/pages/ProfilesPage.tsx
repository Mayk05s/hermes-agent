import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import {
  ClipboardCopy,
  Pencil,
  Plus,
  Settings,
  Trash2,
  Users,
  X,
} from "lucide-react";
import spinners from "unicode-animations";
import { H2 } from "@nous-research/ui/ui/components/typography/h2";
import { api } from "@/lib/api";
import type { ProfileCommunicationStyle, ProfileCommunicationStyleOption, ProfileInfo, ProfileSkillInfo } from "@/lib/api";
import { writeClipboardText } from "@/lib/clipboard";
import { DeleteConfirmDialog } from "@/components/DeleteConfirmDialog";
import { ProfileRouteEditor } from "@/components/ProfileRouteEditor";
import { useToast } from "@nous-research/ui/hooks/use-toast";
import { useConfirmDelete } from "@nous-research/ui/hooks/use-confirm-delete";
import { useModalBehavior } from "@/hooks/useModalBehavior";
import { Toast } from "@nous-research/ui/ui/components/toast";
import { Card, CardContent } from "@nous-research/ui/ui/components/card";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { Input } from "@nous-research/ui/ui/components/input";
import { Label } from "@nous-research/ui/ui/components/label";
import { Checkbox } from "@nous-research/ui/ui/components/checkbox";
import { useI18n } from "@/i18n";
import { usePageHeader } from "@/contexts/usePageHeader";
import { cn, themedBody } from "@/lib/utils";

// Mirrors hermes_cli/profiles.py::_PROFILE_ID_RE so we can reject obviously
// invalid names (uppercase, spaces, …) before round-tripping a doomed POST.
const PROFILE_NAME_RE = /^[a-z0-9][a-z0-9_-]{0,63}$/;

/** Braille unicode spinner (`unicode-animations`); static first frame when reduced motion is preferred. */
function ProfilesLoadingSpinner() {
  const { frames, interval } = spinners.braille;
  const [frameIndex, setFrameIndex] = useState(0);

  useEffect(() => {
    if (
      typeof window !== "undefined" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches
    ) {
      return;
    }
    const id = window.setInterval(
      () => setFrameIndex((i) => (i + 1) % frames.length),
      interval,
    );
    return () => window.clearInterval(id);
  }, [frames.length, interval]);

  return (
    <span
      aria-hidden
      className="inline-block select-none font-mono text-xl leading-none text-muted-foreground"
    >
      {frames[frameIndex]}
    </span>
  );
}

export default function ProfilesPage() {
  const [profiles, setProfiles] = useState<ProfileInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const { toast, showToast } = useToast();
  const { t } = useI18n();
  const { setEnd } = usePageHeader();

  // Create modal
  const [createModalOpen, setCreateModalOpen] = useState(false);
  const [newName, setNewName] = useState("");
  const [cloneFromDefault, setCloneFromDefault] = useState(true);
  const [creating, setCreating] = useState(false);
  const closeCreateModal = useCallback(() => setCreateModalOpen(false), []);
  const createModalRef = useModalBehavior({
    open: createModalOpen,
    onClose: closeCreateModal,
  });

  // Inline rename state
  const [renamingFrom, setRenamingFrom] = useState<string | null>(null);
  const [renameTo, setRenameTo] = useState("");

  const [soulText, setSoulText] = useState("");
  const [soulSaving, setSoulSaving] = useState(false);
  const [profileSettingsFor, setProfileSettingsFor] = useState<string | null>(null);
  const [settingsLoadingFor, setSettingsLoadingFor] = useState<string | null>(null);
  const activeSettingsRequest = useRef<string | null>(null);
  const [styleOptions, setStyleOptions] = useState<ProfileCommunicationStyleOption[]>([]);
  const [stylesByProfile, setStylesByProfile] = useState<Record<string, ProfileCommunicationStyle>>({});
  const [styleDrafts, setStyleDrafts] = useState<Record<string, string>>({});
  const [styleSavingFor, setStyleSavingFor] = useState<string | null>(null);
  const [selectedStyleForEdit, setSelectedStyleForEdit] = useState("");
  const [styleEditorText, setStyleEditorText] = useState("");
  const [styleContentLoadingFor, setStyleContentLoadingFor] = useState<string | null>(null);
  const [styleContentSavingFor, setStyleContentSavingFor] = useState<string | null>(null);
  const [addingStyle, setAddingStyle] = useState(false);
  const [newStyleName, setNewStyleName] = useState("");
  const [styleCreating, setStyleCreating] = useState(false);
  const [profileSkills, setProfileSkills] = useState<Record<string, ProfileSkillInfo[]>>({});
  const [skillSavingKey, setSkillSavingKey] = useState<string | null>(null);
  const [skillSearch, setSkillSearch] = useState("");

  const load = useCallback(async () => {
    try {
      const [res, styleList] = await Promise.all([
        api.getProfiles(),
        api.getCommunicationStyles(),
      ]);
      setProfiles(res.profiles);
      setStyleOptions(styleList.styles);
      const styleEntries = await Promise.all(
        res.profiles.map(async (profile) => {
          try {
            const style = await api.getProfileCommunicationStyle(profile.name);
            return [profile.name, style] as const;
          } catch {
            return [profile.name, { style: "", label: "", file: "", exists: false, content: "" }] as const;
          }
        }),
      );
      const styles = Object.fromEntries(styleEntries);
      setStylesByProfile(styles);
      setStyleDrafts(
        Object.fromEntries(styleEntries.map(([name, style]) => [name, style.style])),
      );
    } catch (e) {
      showToast(`${t.status.error}: ${e}`, "error");
    } finally {
      setLoading(false);
    }
  }, [showToast, t.status.error]);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    if (!selectedStyleForEdit) {
      setStyleEditorText("");
      return;
    }
    let cancelled = false;
    setStyleContentLoadingFor(selectedStyleForEdit);
    api.getCommunicationStyle(selectedStyleForEdit)
      .then((style) => {
        if (!cancelled) {
          setStyleEditorText(style.content || "");
        }
      })
      .catch((e) => {
        if (!cancelled) {
          showToast(`${t.status.error}: ${e}`, "error");
        }
      })
      .finally(() => {
        if (!cancelled) {
          setStyleContentLoadingFor(null);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [selectedStyleForEdit, showToast, t.status.error]);

  const handleCreate = async () => {
    const name = newName.trim();
    if (!name) {
      showToast(t.profiles.nameRequired, "error");
      return;
    }
    if (!PROFILE_NAME_RE.test(name)) {
      showToast(`${t.profiles.invalidName}: ${t.profiles.nameRule}`, "error");
      return;
    }
    setCreating(true);
    try {
      await api.createProfile({ name, clone_from_default: cloneFromDefault });
      showToast(`${t.profiles.created}: ${name}`, "success");
      setNewName("");
      setCreateModalOpen(false);
      load();
    } catch (e) {
      showToast(`${t.status.error}: ${e}`, "error");
    } finally {
      setCreating(false);
    }
  };

  const handleRenameSubmit = async () => {
    if (!renamingFrom) return;
    const target = renameTo.trim();
    if (!target || target === renamingFrom) {
      setRenamingFrom(null);
      setRenameTo("");
      return;
    }
    if (!PROFILE_NAME_RE.test(target)) {
      showToast(`${t.profiles.invalidName}: ${t.profiles.nameRule}`, "error");
      return;
    }
    try {
      await api.renameProfile(renamingFrom, target);
      showToast(
        `${t.profiles.renamed}: ${renamingFrom} → ${target}`,
        "success",
      );
      setRenamingFrom(null);
      setRenameTo("");
      load();
    } catch (e) {
      showToast(`${t.status.error}: ${e}`, "error");
    }
  };

  const openProfileSettings = useCallback(
    async (name: string) => {
      if (profileSettingsFor === name) {
        activeSettingsRequest.current = null;
        setProfileSettingsFor(null);
        return;
      }
      setProfileSettingsFor(name);
      setSettingsLoadingFor(name);
      setSoulText("");
      setSkillSearch("");
      activeSettingsRequest.current = name;
      try {
        const [soul, style, skills] = await Promise.all([
          api.getProfileSoul(name),
          api.getProfileCommunicationStyle(name),
          api.getProfileSkills(name),
        ]);
        if (activeSettingsRequest.current === name) {
          setSoulText(soul.content);
          setStylesByProfile((current) => ({ ...current, [name]: style }));
          setStyleDrafts((current) => ({ ...current, [name]: style.style }));
          setProfileSkills((current) => ({ ...current, [name]: skills.skills }));
        }
      } catch (e) {
        if (activeSettingsRequest.current === name) {
          showToast(`${t.status.error}: ${e}`, "error");
        }
      } finally {
        if (activeSettingsRequest.current === name) {
          setSettingsLoadingFor(null);
        }
      }
    },
    [profileSettingsFor, showToast, t.status.error],
  );

  const handleSaveSoul = async (name: string) => {
    setSoulSaving(true);
    try {
      await api.updateProfileSoul(name, soulText);
      showToast(`${t.profiles.soulSaved}: ${name}`, "success");
    } catch (e) {
      showToast(`${t.status.error}: ${e}`, "error");
    } finally {
      setSoulSaving(false);
    }
  };

  const handleSaveStyle = async (name: string) => {
    setStyleSavingFor(name);
    try {
      const style = await api.updateProfileCommunicationStyle(name, styleDrafts[name] || "");
      setStylesByProfile((current) => ({ ...current, [name]: style }));
      setStyleDrafts((current) => ({ ...current, [name]: style.style }));
      showToast(`Communication style saved: ${name}`, "success");
    } catch (e) {
      showToast(`${t.status.error}: ${e}`, "error");
    } finally {
      setStyleSavingFor(null);
    }
  };

  const handleStyleDraftChange = (name: string, styleName: string) => {
    setStyleDrafts((current) => ({ ...current, [name]: styleName }));
  };

  const handleSaveStyleContent = async () => {
    const styleName = selectedStyleForEdit;
    if (!styleName) return;
    setStyleContentSavingFor(styleName);
    try {
      const style = await api.updateCommunicationStyle(styleName, styleEditorText);
      setStylesByProfile((current) => {
        const next = { ...current };
        for (const [profileName, currentStyle] of Object.entries(next)) {
          if (currentStyle.style === style.style) {
            next[profileName] = { ...currentStyle, label: style.label, content: style.content, exists: style.exists };
          }
        }
        return next;
      });
      setStyleOptions((current) =>
        current.map((option) =>
          option.style === style.style
            ? { ...option, label: style.label, file: style.file, exists: style.exists }
            : option,
        ),
      );
      showToast(`Communication style file saved: ${style.label || style.style}`, "success");
    } catch (e) {
      showToast(`${t.status.error}: ${e}`, "error");
    } finally {
      setStyleContentSavingFor(null);
    }
  };

  const handleCreateCommunicationStyle = async () => {
    const styleName = newStyleName.trim().toLowerCase();
    if (!PROFILE_NAME_RE.test(styleName)) {
      showToast(`Invalid style name: ${t.profiles.nameRule}`, "error");
      return;
    }
    setStyleCreating(true);
    try {
      const style = await api.createCommunicationStyle(styleName);
      setStyleOptions((current) => [...current, style].sort((a, b) => (
        (a.style !== "default" && b.style === "default" ? 1 : 0) ||
        (a.style === "default" && b.style !== "default" ? -1 : 0) ||
        a.label.localeCompare(b.label)
      )));
      setSelectedStyleForEdit(style.style);
      setStyleEditorText(style.content || "");
      setNewStyleName("");
      setAddingStyle(false);
      showToast(`Communication style created: ${style.label || style.style}`, "success");
    } catch (e) {
      showToast(`${t.status.error}: ${e}`, "error");
    } finally {
      setStyleCreating(false);
    }
  };

  const handleToggleProfileSkill = async (profileName: string, skill: ProfileSkillInfo, enabled: boolean) => {
    const key = `${profileName}:${skill.name}`;
    setSkillSavingKey(key);
    setProfileSkills((current) => ({
      ...current,
      [profileName]: (current[profileName] || []).map((item) =>
        item.name === skill.name ? { ...item, enabled } : item,
      ),
    }));
    try {
      await api.toggleProfileSkill(profileName, skill.name, enabled);
      showToast(`${enabled ? "Skill enabled" : "Skill disabled"}: ${skill.name}`, "success");
    } catch (e) {
      setProfileSkills((current) => ({
        ...current,
        [profileName]: (current[profileName] || []).map((item) =>
          item.name === skill.name ? { ...item, enabled: !enabled } : item,
        ),
      }));
      showToast(`${t.status.error}: ${e}`, "error");
    } finally {
      setSkillSavingKey(null);
    }
  };

  const handleCopyTerminalCommand = async (profile: ProfileInfo) => {
    const cmd = profile.is_default ? "hermes setup" : `${profile.name} setup`;
    try {
      await writeClipboardText(cmd);
      showToast(`Setup command copied: ${cmd}`, "success");
    } catch (e) {
      showToast(`Could not copy setup command: ${e}`, "error");
    }
  };

  const profileDelete = useConfirmDelete<string>({
    onDelete: useCallback(
      async (name: string) => {
        try {
          await api.deleteProfile(name);
          showToast(`${t.profiles.deleted}: ${name}`, "success");
          load();
        } catch (e) {
          showToast(`${t.status.error}: ${e}`, "error");
          throw e;
        }
      },
      [load, showToast, t.profiles.deleted, t.status.error],
    ),
  });

  const pendingName = profileDelete.pendingId;

  // Put "Create" button in page header
  useLayoutEffect(() => {
    setEnd(
      <Button
        className="uppercase"
        size="sm"
        onClick={() => setCreateModalOpen(true)}
      >
        {t.common.create}
      </Button>,
    );
    return () => {
      setEnd(null);
    };
  }, [setEnd, t.common.create, loading]);

  function filteredSkillsForProfile(name: string): ProfileSkillInfo[] {
    const query = skillSearch.trim().toLowerCase();
    const skills = profileSkills[name] || [];
    if (!query) return skills;
    return skills.filter((skill) =>
      skill.name.toLowerCase().includes(query) ||
      skill.description.toLowerCase().includes(query) ||
      (skill.category || "uncategorized").toLowerCase().includes(query),
    );
  }

  function groupedSkillsForProfile(name: string): Array<[string, ProfileSkillInfo[]]> {
    const groups = new Map<string, ProfileSkillInfo[]>();
    for (const skill of filteredSkillsForProfile(name)) {
      const category = skill.category || "uncategorized";
      const bucket = groups.get(category) || [];
      bucket.push(skill);
      groups.set(category, bucket);
    }
    return Array.from(groups.entries()).sort(([a], [b]) => a.localeCompare(b));
  }

  if (loading) {
    return (
      <div
        aria-busy="true"
        aria-live="polite"
        className="flex items-center justify-center py-24"
      >
        <span className="sr-only">{t.common.loading}</span>

        <ProfilesLoadingSpinner />
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-6">
      <Toast toast={toast} />

      <DeleteConfirmDialog
        open={profileDelete.isOpen}
        onCancel={profileDelete.cancel}
        onConfirm={profileDelete.confirm}
        title={t.profiles.confirmDeleteTitle}
        description={
          pendingName
            ? t.profiles.confirmDeleteMessage.replace("{name}", pendingName)
            : t.profiles.confirmDeleteMessage
        }
        loading={profileDelete.isDeleting}
      />

      {/* Create profile modal */}
      {createModalOpen && (
        <div
          ref={createModalRef}
          className="fixed inset-0 z-[100] flex items-center justify-center bg-background/85 backdrop-blur-sm p-4"
          onClick={(e) =>
            e.target === e.currentTarget && setCreateModalOpen(false)
          }
          role="dialog"
          aria-modal="true"
          aria-labelledby="create-profile-title"
        >
          <div className={cn(themedBody, "relative w-full max-w-md border border-border bg-card shadow-2xl flex flex-col")}>
            <Button
              ghost
              size="icon"
              onClick={() => setCreateModalOpen(false)}
              className="absolute right-2 top-2 text-muted-foreground hover:text-foreground"
              aria-label="Close"
            >
              <X />
            </Button>

            <header className="p-5 pb-3 border-b border-border">
              <h2
                id="create-profile-title"
                className="font-mondwest text-display text-base tracking-wider"
              >
                {t.profiles.newProfile}
              </h2>
            </header>

            <div className="p-5 grid gap-4">
              <div className="grid gap-2">
                <Label htmlFor="profile-name">{t.profiles.name}</Label>
                <Input
                  id="profile-name"
                  autoFocus
                  placeholder={t.profiles.namePlaceholder}
                  value={newName}
                  onChange={(e) => setNewName(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") handleCreate();
                  }}
                  aria-invalid={
                    newName.trim() !== "" &&
                    !PROFILE_NAME_RE.test(newName.trim())
                  }
                />
                <p className="text-xs text-muted-foreground">
                  {t.profiles.nameRule}
                </p>
              </div>

              <div className="flex items-center gap-2.5">
                <Checkbox
                  checked={cloneFromDefault}
                  id="clone-from-default"
                  onCheckedChange={(checked) =>
                    setCloneFromDefault(checked === true)
                  }
                />

                <Label
                  className="font-mondwest normal-case tracking-normal text-sm cursor-pointer"
                  htmlFor="clone-from-default"
                >
                  {t.profiles.cloneFromDefault}
                </Label>
              </div>

              <div className="flex justify-end">
                <Button
                  className="uppercase"
                  size="sm"
                  onClick={handleCreate}
                  disabled={creating}
                >
                  {creating ? t.common.creating : t.common.create}
                </Button>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* List */}
      <div className="flex flex-col gap-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <H2
            variant="sm"
            className="flex items-center gap-2 text-muted-foreground"
          >
            <Users className="h-4 w-4" />
            {t.profiles.allProfiles} ({profiles.length})
          </H2>
          <Button
            className="uppercase"
            size="sm"
            onClick={() => setCreateModalOpen(true)}
          >
            {t.common.create}
          </Button>
        </div>

        {profiles.length === 0 && (
          <Card>
            <CardContent className="py-8 text-center text-sm text-muted-foreground">
              {t.profiles.noProfiles}
            </CardContent>
          </Card>
        )}

        {profiles.map((p) => {
          const isRenaming = renamingFrom === p.name;
          const isSettingsOpen = profileSettingsFor === p.name;
          const profileSkillList = profileSkills[p.name] || [];
          const enabledSkills = profileSkillList.filter((skill) => skill.enabled).length;
          const groupedSkills = groupedSkillsForProfile(p.name);
          return (
            <Card key={p.name}>
              <CardContent className="flex items-start gap-4 py-4">
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 mb-1 flex-wrap">
                    {isRenaming ? (
                      <Input
                        autoFocus
                        value={renameTo}
                        onChange={(e) => setRenameTo(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") handleRenameSubmit();
                          if (e.key === "Escape") setRenamingFrom(null);
                        }}
                        aria-invalid={
                          renameTo.trim() !== "" &&
                          renameTo.trim() !== p.name &&
                          !PROFILE_NAME_RE.test(renameTo.trim())
                        }
                        className="max-w-xs"
                      />
                    ) : (
                      <span className="font-medium text-sm truncate">
                        {p.name}
                      </span>
                    )}
                    {p.is_default && (
                      <Badge tone="secondary">{t.profiles.defaultBadge}</Badge>
                    )}
                    {p.has_env && (
                      <Badge tone="outline">{t.profiles.hasEnv}</Badge>
                    )}
                  </div>
                  {isRenaming &&
                    (() => {
                      const trimmed = renameTo.trim();
                      const invalid =
                        trimmed !== "" &&
                        trimmed !== p.name &&
                        !PROFILE_NAME_RE.test(trimmed);
                      return (
                        <p
                          className={
                            "text-xs mb-1 " +
                            (invalid
                              ? "text-destructive"
                              : "text-muted-foreground")
                          }
                        >
                          {invalid
                            ? `${t.profiles.invalidName}: ${t.profiles.nameRule}`
                            : t.profiles.nameRule}
                        </p>
                      );
                    })()}
                  <div className="flex items-center gap-4 text-xs text-muted-foreground flex-wrap">
                    {p.model && (
                      <span>
                        {t.profiles.model}: {p.model}
                        {p.provider ? ` (${p.provider})` : ""}
                      </span>
                    )}
                    <span>
                      {t.profiles.skills}: {p.skill_count}
                    </span>
                    <span className="font-mono truncate max-w-[28rem]">
                      {p.path}
                    </span>
                  </div>
                </div>

                <div className="flex items-center gap-1 shrink-0">
                  {isRenaming ? (
                    <>
                      <Button size="sm" onClick={handleRenameSubmit}>
                        {t.common.save}
                      </Button>
                      <Button
                        size="sm"
                        ghost
                        onClick={() => setRenamingFrom(null)}
                      >
                        {t.common.cancel}
                      </Button>
                    </>
                  ) : (
                    <>
                      <Button
                        ghost
                        size="sm"
                        className="gap-2"
                        title="Profile settings"
                        aria-label="Profile settings"
                        onClick={() => openProfileSettings(p.name)}
                      >
                        <Settings className="h-4 w-4" />
                        Settings
                      </Button>
                      <Button
                        ghost
                        size="sm"
                        className="gap-2"
                        title="Copy shell setup command for this profile"
                        aria-label="Copy shell setup command for this profile"
                        onClick={() => handleCopyTerminalCommand(p)}
                      >
                        <ClipboardCopy className="h-4 w-4" />
                        Copy setup cmd
                      </Button>
                      {!p.is_default && (
                        <Button
                          ghost
                          size="icon"
                          title={t.profiles.rename}
                          aria-label={t.profiles.rename}
                          onClick={() => {
                            setRenamingFrom(p.name);
                            setRenameTo(p.name);
                          }}
                        >
                          <Pencil className="h-4 w-4" />
                        </Button>
                      )}
                      {!p.is_default && (
                        <Button
                          ghost
                          size="icon"
                          title={t.common.delete}
                          aria-label={t.common.delete}
                          onClick={() => profileDelete.requestDelete(p.name)}
                        >
                          <Trash2 className="h-4 w-4 text-destructive" />
                        </Button>
                      )}
                    </>
                  )}
                </div>
              </CardContent>

              <div className="border-t border-border px-4 py-3">
                <div className="flex flex-wrap items-center gap-2 text-sm">
                  <span className="text-muted-foreground">Communication style:</span>
                  <span className="font-medium">
                    {stylesByProfile[p.name]?.label || stylesByProfile[p.name]?.style || "None"}
                  </span>
                  <span className={stylesByProfile[p.name]?.exists ? "text-xs text-success" : "text-xs text-warning"}>
                    {stylesByProfile[p.name]?.exists ? "available" : "missing"}
                  </span>
                </div>
              </div>

              {isSettingsOpen && (
                <div className="border-t border-border px-4 py-4">
                  {settingsLoadingFor === p.name ? (
                    <div className="flex items-center gap-2 text-sm text-muted-foreground">
                      <ProfilesLoadingSpinner />
                      Loading profile settings...
                    </div>
                  ) : (
                    <div className="grid gap-5">
                      <section className="grid gap-3">
                        <div className="flex flex-wrap items-end gap-3">
                          <Label
                            htmlFor={`style-selector-${p.name}`}
                            className="grid min-w-[260px] max-w-sm gap-1 text-xs text-muted-foreground"
                          >
                            Communication style
                            <select
                              id={`style-selector-${p.name}`}
                              className="h-9 border border-input bg-transparent px-2 text-sm text-foreground"
                              value={styleDrafts[p.name] ?? ""}
                              onChange={(e) => handleStyleDraftChange(p.name, e.target.value)}
                            >
                              <option value="">No style</option>
                              {styleOptions.map((style) => (
                                <option key={style.style} value={style.style}>
                                  {style.label}
                                </option>
                              ))}
                            </select>
                          </Label>
                          <Button
                            size="sm"
                            className="uppercase"
                            onClick={() => handleSaveStyle(p.name)}
                            disabled={styleSavingFor === p.name}
                          >
                            {styleSavingFor === p.name ? t.common.saving : "Save style"}
                          </Button>
                        </div>
                      </section>

                      <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(320px,0.9fr)]">
                        <section className="grid min-w-0 content-start gap-2">
                          <Label
                            htmlFor={`soul-editor-${p.name}`}
                            className="font-mondwest text-display text-xs tracking-wider text-muted-foreground"
                          >
                            SOUL.md
                          </Label>
                          <textarea
                            id={`soul-editor-${p.name}`}
                            className="min-h-[420px] w-full resize-y border border-input bg-transparent px-3 py-2 text-sm font-mono leading-relaxed shadow-sm placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                            rows={18}
                            placeholder={t.profiles.soulPlaceholder}
                            value={soulText}
                            onChange={(e) => setSoulText(e.target.value)}
                          />
                          <div>
                            <Button
                              size="sm"
                              className="uppercase"
                              onClick={() => handleSaveSoul(p.name)}
                              disabled={soulSaving}
                            >
                              {soulSaving ? t.common.saving : t.common.save}
                            </Button>
                          </div>
                        </section>

                        <section className="grid min-w-0 content-start gap-3">
                          <div className="flex flex-wrap items-end justify-between gap-3">
                            <div>
                              <div className="font-mondwest text-display text-xs tracking-wider text-muted-foreground">
                                Skills
                              </div>
                              <div className="text-xs text-muted-foreground">
                                {profileSkillList.length
                                  ? `${enabledSkills}/${profileSkillList.length} enabled for this profile`
                                  : "No profile skills installed."}
                              </div>
                            </div>
                            <Input
                              className="w-full sm:max-w-[240px]"
                              placeholder="Search skills"
                              value={skillSearch}
                              onChange={(e) => setSkillSearch(e.target.value)}
                            />
                          </div>

                          {groupedSkills.length === 0 ? (
                            <div className="border border-border px-3 py-4 text-sm text-muted-foreground">
                              No skills match the current filter.
                            </div>
                          ) : (
                            <div className="max-h-[420px] overflow-y-auto border border-border">
                              {groupedSkills.map(([category, skills]) => (
                                <div key={category} className="border-b border-border last:border-b-0">
                                  <div className="bg-muted/20 px-3 py-2 text-xs font-mondwest text-display tracking-wider text-muted-foreground">
                                    {category} ({skills.filter((skill) => skill.enabled).length}/{skills.length})
                                  </div>
                                  <div className="divide-y divide-border/70">
                                    {skills.map((skill) => {
                                      const savingKey = `${p.name}:${skill.name}`;
                                      return (
                                        <label key={skill.name} className="flex cursor-pointer items-start gap-3 px-3 py-2">
                                          <Checkbox
                                            checked={skill.enabled}
                                            disabled={skillSavingKey === savingKey}
                                            onCheckedChange={(checked) =>
                                              handleToggleProfileSkill(p.name, skill, checked === true)
                                            }
                                          />
                                          <span className="grid gap-1">
                                            <span className="text-sm font-medium">{skill.name}</span>
                                            {skill.description ? (
                                              <span className="text-xs leading-relaxed text-muted-foreground">
                                                {skill.description}
                                              </span>
                                            ) : null}
                                          </span>
                                        </label>
                                      );
                                    })}
                                  </div>
                                </div>
                              ))}
                            </div>
                          )}
                        </section>
                      </div>
                    </div>
                  )}
                </div>
              )}
            </Card>
          );
        })}
      </div>

      <ProfileRouteEditor profiles={profiles} />

      <Card>
        <CardContent className="grid gap-4 py-4">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <H2 variant="sm" className="text-muted-foreground">
                Communication Styles
              </H2>
              <div className="mt-1 text-xs text-muted-foreground">
                Shared style files that profiles can select.
              </div>
            </div>
            <Button
              size="sm"
              className="uppercase"
              onClick={() => {
                setAddingStyle((current) => !current);
                setSelectedStyleForEdit("");
              }}
            >
              <Plus className="h-4 w-4" />
              Add style
            </Button>
          </div>

          {addingStyle ? (
            <div className="flex flex-wrap items-end gap-2 border border-border bg-muted/10 p-3">
              <Label className="grid min-w-[240px] gap-1 text-xs text-muted-foreground">
                Style id
                <Input
                  placeholder="work-direct"
                  value={newStyleName}
                  onChange={(e) => setNewStyleName(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") handleCreateCommunicationStyle();
                    if (e.key === "Escape") setAddingStyle(false);
                  }}
                />
              </Label>
              <Button
                size="sm"
                className="uppercase"
                onClick={handleCreateCommunicationStyle}
                disabled={styleCreating}
              >
                {styleCreating ? t.common.creating : t.common.create}
              </Button>
              <Button
                size="sm"
                ghost
                onClick={() => {
                  setAddingStyle(false);
                  setNewStyleName("");
                }}
              >
                {t.common.cancel}
              </Button>
            </div>
          ) : null}

          <div className="overflow-x-auto border border-border">
            <table className="w-full min-w-[760px] text-sm">
              <thead className="text-xs text-muted-foreground">
                <tr>
                  <th className="px-3 py-2 text-left">Style</th>
                  <th className="px-3 py-2 text-left">Used by</th>
                  <th className="px-3 py-2 text-left">File</th>
                  <th className="px-3 py-2 text-right">Actions</th>
                </tr>
              </thead>
              <tbody>
                {styleOptions.length === 0 ? (
                  <tr>
                    <td className="px-3 py-4 text-muted-foreground" colSpan={4}>
                      No communication styles configured.
                    </td>
                  </tr>
                ) : styleOptions.map((style, index) => {
                  const usedBy = profiles
                    .filter((profile) => stylesByProfile[profile.name]?.style === style.style)
                    .map((profile) => profile.name);
                  const isEditing = selectedStyleForEdit === style.style;
                  return (
                    <tr
                      key={style.style}
                      className={`border-t border-border align-top hover:bg-muted/20 ${index % 2 ? "bg-muted/5" : "bg-transparent"}`}
                    >
                      <td className="px-3 py-2">
                        <div className="font-medium">{style.label}</div>
                        <div className="font-mono text-xs text-muted-foreground">{style.style}</div>
                      </td>
                      <td className="px-3 py-2">
                        {usedBy.length ? (
                          <div className="flex flex-wrap gap-1">
                            {usedBy.map((name) => (
                              <Badge key={name} tone="outline">{name}</Badge>
                            ))}
                          </div>
                        ) : (
                          <span className="text-muted-foreground">-</span>
                        )}
                      </td>
                      <td className="px-3 py-2">
                        <div className="font-mono text-xs text-muted-foreground">{style.file}</div>
                      </td>
                      <td className="px-3 py-2 text-right">
                        <Button
                          type="button"
                          ghost
                          size="sm"
                          onClick={() => {
                            setAddingStyle(false);
                            setSelectedStyleForEdit(isEditing ? "" : style.style);
                          }}
                        >
                          {isEditing ? "Close" : "Edit"}
                        </Button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>

          {selectedStyleForEdit ? (
            <div className="grid gap-2 border border-border p-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div>
                  <div className="font-medium">
                    {styleOptions.find((style) => style.style === selectedStyleForEdit)?.label || selectedStyleForEdit}
                  </div>
                  <div className="font-mono text-xs text-muted-foreground">
                    {styleOptions.find((style) => style.style === selectedStyleForEdit)?.file || ""}
                  </div>
                </div>
                <Button
                  size="sm"
                  ghost
                  onClick={() => setSelectedStyleForEdit("")}
                >
                  Close editor
                </Button>
              </div>
              <textarea
                className="min-h-[360px] w-full resize-y border border-input bg-transparent px-3 py-2 text-sm font-mono leading-relaxed shadow-sm placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:opacity-50"
                rows={16}
                placeholder="Style markdown"
                value={styleEditorText}
                onChange={(e) => setStyleEditorText(e.target.value)}
                disabled={styleContentLoadingFor === selectedStyleForEdit}
              />
              <div className="flex justify-end">
                <Button
                  size="sm"
                  className="uppercase"
                  onClick={handleSaveStyleContent}
                  disabled={styleContentSavingFor === selectedStyleForEdit}
                >
                  {styleContentSavingFor === selectedStyleForEdit ? t.common.saving : "Save style file"}
                </Button>
              </div>
            </div>
          ) : null}
        </CardContent>
      </Card>
    </div>
  );
}
