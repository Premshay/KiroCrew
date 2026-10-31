import { useId, useState, type ComponentType, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, ChevronRight, KeyRound } from "lucide-react";
import { useTranslation } from "react-i18next";
import { api } from "../api/client";
import type {
  SlotAccountState,
  SlotAccountSwitchResult,
} from "../api/client/chatSlotSettings";
import { store, useAppSelector } from "../store";
import { setAgentSwitchNotice } from "../store/chatSlice";
import ErrorNotice, {
  ErrorNoticeMenuItem,
  type ErrorNoticeMenuItemComponent,
} from "./ErrorNotice";
import {
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
} from "./ui/dropdown-menu";
import {
  ContextMenuItem,
  ContextMenuLabel,
  ContextMenuRadioGroup,
  ContextMenuRadioItem,
  ContextMenuSeparator,
  ContextMenuSub,
  ContextMenuSubContent,
  ContextMenuSubTrigger,
} from "./ui/context-menu";

/** Keyed by the session's current agent too: eligibility follows the backend the
 *  session runs on now, so an agent switch must refetch rather than reuse a cached
 *  answer for the previous seat. */
export const sessionAccountQueryKey = (slotKey: string, agentIdentity = "") =>
  ["slot-account", slotKey, agentIdentity] as const;

type RadioGroupComponent = ComponentType<{
  value?: string;
  onValueChange?: (value: string) => void;
  children?: ReactNode;
}>;
type RadioItemComponent = ComponentType<{
  value: string;
  disabled?: boolean;
  onSelect?: (event: Event) => void;
  children?: ReactNode;
}>;
type SeparatorComponent = ComponentType<{ className?: string }>;

export function SessionAccountSubmenu({
  slotKey,
  variant,
}: {
  slotKey: string;
  variant: "dropdown" | "context";
}) {
  const { t: i18nT } = useTranslation();
  const queryClient = useQueryClient();
  const agentIdentity = useAppSelector((s) => {
    const slot = s.dashboard.slots.find((x) => x.key === slotKey);
    return `${slot?.runtime_agent ?? ""}|${slot?.agent ?? ""}`;
  });
  const errorId = useId();
  const loadErrorId = useId();
  const [pendingAccount, setPendingAccount] = useState<string | null>(null);
  const [saveError, setSaveError] = useState("");
  const [toast, setToast] = useState("");

  const query = useQuery({
    queryKey: sessionAccountQueryKey(slotKey, agentIdentity),
    queryFn: () => api.chatSlotAccount(slotKey),
    staleTime: 15_000,
  });

  const mutation = useMutation({
    mutationFn: (account: string) => api.setChatSlotAccount(slotKey, account),
    onMutate: () => {
      setSaveError("");
    },
    onSuccess: (res: SlotAccountSwitchResult) => {
      setPendingAccount(null);
      queryClient.setQueryData<SlotAccountState>(
        sessionAccountQueryKey(slotKey, agentIdentity),
        {
          available: res.available,
          eligible: res.eligible,
          reason: res.reason,
          current: res.current,
          default: res.default,
          default_label: res.default_label,
          accounts: res.accounts,
        },
      );
      const chosen = res.accounts.find((a) => a.id === res.account);
      const label = chosen?.label || res.account;
      const message = res.deferred
        ? i18nT("components.sessionAccount.deferred_toast")
        : i18nT("components.sessionAccount.switched_toast", { account: label });
      setToast(message);
      store.dispatch(setAgentSwitchNotice(message));
    },
    onError: (err) => {
      setPendingAccount(null);
      setSaveError(
        err instanceof Error && err.message
          ? err.message
          : i18nT("components.sessionAccount.save_failed"),
      );
      queryClient.invalidateQueries({
        queryKey: sessionAccountQueryKey(slotKey, agentIdentity),
      });
    },
  });

  const Item: ErrorNoticeMenuItemComponent =
    variant === "context" ? ContextMenuItem : DropdownMenuItem;
  const Sub = variant === "context" ? ContextMenuSub : DropdownMenuSub;
  const SubTrigger =
    variant === "context" ? ContextMenuSubTrigger : DropdownMenuSubTrigger;
  const SubContent =
    variant === "context" ? ContextMenuSubContent : DropdownMenuSubContent;
  const Label = variant === "context" ? ContextMenuLabel : DropdownMenuLabel;
  const RadioGroup: RadioGroupComponent =
    variant === "context" ? ContextMenuRadioGroup : DropdownMenuRadioGroup;
  const RadioItem: RadioItemComponent =
    variant === "context" ? ContextMenuRadioItem : DropdownMenuRadioItem;
  const Separator: SeparatorComponent =
    variant === "context" ? ContextMenuSeparator : DropdownMenuSeparator;

  const data = query.data;
  if (!data && !query.isError) {
    return null;
  }
  if (data && (!data.available || data.accounts.length === 0)) {
    return null;
  }

  if (data && !data.eligible) {
    const reasonText =
      data.reason || i18nT("components.sessionAccount.ineligible_default");
    return (
      <Item disabled title={reasonText} data-testid="session-account-disabled">
        <KeyRound size={13} className="shrink-0 text-muted" />
        <span>{i18nT("components.sessionAccount.label")}</span>
        <span className="ml-auto max-w-[170px] truncate text-[10px] text-muted">
          {reasonText}
        </span>
      </Item>
    );
  }

  const loadError = query.isError
    ? query.error instanceof Error
      ? query.error.message
      : String(query.error)
    : "";

  const keepOpen = (event: Event) => {
    event.preventDefault();
  };

  const choose = (next: string) => {
    if (!data || next === data.current || mutation.isPending) return;
    setSaveError("");
    setToast("");
    setPendingAccount(next);
  };

  const pendingOption = data?.accounts.find((a) => a.id === pendingAccount);
  const pendingLabel = pendingOption?.label || pendingAccount || "";
  const defaultLabel =
    data?.default_label ||
    data?.accounts.find((a) => a.id === data.default)?.label ||
    data?.default ||
    "";

  return (
    <Sub>
      <SubTrigger
        title={i18nT("components.sessionAccount.hint")}
        data-testid="session-account-submenu"
      >
        <KeyRound size={13} className="shrink-0 text-muted" />
        <span className="flex-1">
          {i18nT("components.sessionAccount.label")}
        </span>
        <ChevronRight size={12} className="text-muted" />
      </SubTrigger>
      <SubContent className="min-w-[230px]">
        {loadError && (
          <>
            <div className="max-w-[260px] px-2 py-1.5">
              <ErrorNotice
                id={loadErrorId}
                variant="inline"
                className="flex-wrap"
                title={i18nT("components.sessionAccount.load_failed")}
                message={loadError}
                messagePlacement="below"
                testId="session-account-load-error"
              />
            </div>
            <ErrorNoticeMenuItem
              Item={Item}
              message={loadError}
              describedBy={loadErrorId}
            />
          </>
        )}
        {data && (
          <>
            <RadioGroup value={data.current} onValueChange={choose}>
              {data.accounts.map((option) => {
                const checked = data.current === option.id;
                return (
                  <RadioItem
                    key={option.id}
                    value={option.id}
                    disabled={mutation.isPending}
                    onSelect={keepOpen}
                  >
                    <span className="truncate">{option.label}</span>
                    {checked ? (
                      <Check
                        size={13}
                        className="ml-auto shrink-0 text-accent"
                        aria-hidden="true"
                      />
                    ) : null}
                  </RadioItem>
                );
              })}
            </RadioGroup>
            <Separator />
            <Label
              className="text-[11px] font-normal text-muted"
              data-testid="session-account-default-info"
            >
              {i18nT("components.sessionAccount.default_for_new_sessions", {
                account: defaultLabel,
              })}
            </Label>
            {pendingAccount && (
              <>
                <Separator />
                <div
                  className="max-w-[260px] px-2 py-1.5 text-[11px]"
                  data-testid="session-account-confirm-box"
                >
                  <p className="mb-2 leading-snug text-text">
                    {i18nT("components.sessionAccount.confirm_switch", {
                      account: pendingLabel,
                    })}
                  </p>
                  <div className="flex items-center justify-end gap-1.5">
                    <button
                      type="button"
                      className="rounded px-2 py-0.5 text-[11px] text-muted hover:bg-bg-hover"
                      data-testid="session-account-cancel"
                      disabled={mutation.isPending}
                      onClick={(e) => {
                        e.stopPropagation();
                        setPendingAccount(null);
                      }}
                    >
                      {i18nT("components.sessionAccount.cancel_button")}
                    </button>
                    <button
                      type="button"
                      className="rounded bg-accent px-2 py-0.5 text-[11px] font-medium text-white hover:opacity-90"
                      data-testid="session-account-confirm"
                      disabled={mutation.isPending}
                      onClick={(e) => {
                        e.stopPropagation();
                        mutation.mutate(pendingAccount);
                      }}
                    >
                      {i18nT("components.sessionAccount.confirm_button")}
                    </button>
                  </div>
                </div>
              </>
            )}
            {toast && (
              <>
                <Separator />
                <div
                  role="status"
                  className="px-2 py-1 text-[11px] text-accent"
                  data-testid="session-account-toast"
                >
                  {toast}
                </div>
              </>
            )}
          </>
        )}
        {saveError && (
          <>
            <div className="max-w-[260px] px-2 py-1.5">
              <ErrorNotice
                id={errorId}
                variant="inline"
                className="flex-wrap"
                title={i18nT("components.sessionAccount.save_failed")}
                message={saveError}
                messagePlacement="below"
                testId="session-account-save-error"
              />
            </div>
            <ErrorNoticeMenuItem
              Item={Item}
              message={saveError}
              describedBy={errorId}
            />
          </>
        )}
      </SubContent>
    </Sub>
  );
}
