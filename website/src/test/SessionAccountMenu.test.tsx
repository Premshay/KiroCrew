import { type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Provider } from "react-redux";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ThemeProvider } from "../hooks/useTheme";
import { store } from "../store";
import { sseSlots, updateSlot } from "../store/dashboardSlice";
import { api } from "../api/client";
import type {
  SlotAccountState,
  SlotAccountSwitchResult,
} from "../api/client/chatSlotSettings";
import { SessionAccountSubmenu } from "../components/SessionAccountMenu";
import SessionActionsMenu from "../components/SessionActionsMenu";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuTrigger,
} from "../components/ui/dropdown-menu";
import {
  ContextMenu,
  ContextMenuContent,
  ContextMenuTrigger,
} from "../components/ui/context-menu";

const SLOT = "chat-claude-seat";

const ELIGIBLE_STATE: SlotAccountState = {
  available: true,
  eligible: true,
  reason: "",
  current: "personal",
  default: "personal",
  default_label: "Personal",
  accounts: [
    { id: "personal", label: "Personal" },
    { id: "atlomy", label: "Atlomy" },
  ],
};

function renderWithProviders(ui: ReactNode) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <ThemeProvider>
          <MemoryRouter>{ui}</MemoryRouter>
        </ThemeProvider>
      </QueryClientProvider>
    </Provider>,
  );
}

function renderSubmenu(variant: "dropdown" | "context" = "dropdown") {
  if (variant === "context") {
    const view = renderWithProviders(
      <ContextMenu>
        <ContextMenuTrigger>target</ContextMenuTrigger>
        <ContextMenuContent forceMount>
          <SessionAccountSubmenu slotKey={SLOT} variant="context" />
        </ContextMenuContent>
      </ContextMenu>,
    );
    act(() => {
      fireEvent.contextMenu(screen.getByText("target"));
    });
    return view;
  }
  return renderWithProviders(
    <DropdownMenu defaultOpen>
      <DropdownMenuTrigger>open</DropdownMenuTrigger>
      <DropdownMenuContent forceMount>
        <SessionAccountSubmenu slotKey={SLOT} variant="dropdown" />
      </DropdownMenuContent>
    </DropdownMenu>,
  );
}

describe("SessionAccountSubmenu", () => {
  beforeEach(() => {
    vi.spyOn(api, "chatFolders").mockResolvedValue([]);
    vi.spyOn(api, "chatSlotAccount").mockResolvedValue(ELIGIBLE_STATE);
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("hides the menu item when the edition contributes no accounts", async () => {
    vi.spyOn(api, "chatSlotAccount").mockResolvedValue({
      available: false,
      eligible: false,
      reason: "",
      current: "",
      default: "",
      default_label: "",
      accounts: [],
    });
    renderSubmenu("dropdown");
    await waitFor(() => expect(api.chatSlotAccount).toHaveBeenCalledWith(SLOT));
    expect(screen.queryByTestId("session-account-submenu")).toBeNull();
  });

  it("disables the menu item and explains why when the session is not a Claude subscription seat", async () => {
    vi.spyOn(api, "chatSlotAccount").mockResolvedValue({
      ...ELIGIBLE_STATE,
      eligible: false,
      reason: "Not a Claude subscription seat (Local Fleet)",
    });
    renderSubmenu("dropdown");
    const item = await screen.findByTestId("session-account-disabled");
    expect(item).toHaveAttribute("data-disabled");
    expect(item.textContent).toContain("Claude account");
    expect(item.textContent).toContain(
      "Not a Claude subscription seat (Local Fleet)",
    );
  });

  it.each(["dropdown", "context"] as const)(
    "renders inside SessionActionsMenu (%s variant) for both sidebar and header menus",
    async (variant) => {
      if (variant === "context") {
        renderWithProviders(
          <ContextMenu>
            <ContextMenuTrigger>row</ContextMenuTrigger>
            <ContextMenuContent forceMount>
              <SessionActionsMenu slotKey={SLOT} variant="context" />
            </ContextMenuContent>
          </ContextMenu>,
        );
        act(() => {
          fireEvent.contextMenu(screen.getByText("row"));
        });
      } else {
        renderWithProviders(
          <DropdownMenu defaultOpen>
            <DropdownMenuTrigger>header</DropdownMenuTrigger>
            <DropdownMenuContent forceMount>
              <SessionActionsMenu slotKey={SLOT} variant="dropdown" />
            </DropdownMenuContent>
          </DropdownMenu>,
        );
      }
      const trigger = await screen.findByTestId("session-account-submenu");
      expect(trigger.textContent).toContain("Claude account");
    },
  );

  it("shows Personal and Atlomy radios, default info, confirms in-menu, and toasts on immediate switch", async () => {
    const user = userEvent.setup();
    const setSpy = vi.spyOn(api, "setChatSlotAccount").mockResolvedValue({
      ...ELIGIBLE_STATE,
      ok: true,
      account: "atlomy",
      current: "atlomy",
      deferred: false,
    } satisfies SlotAccountSwitchResult);

    renderSubmenu("dropdown");
    const subTrigger = await screen.findByTestId("session-account-submenu");
    await act(async () => {
      subTrigger.focus();
      fireEvent.keyDown(subTrigger, { key: "ArrowRight" });
    });

    const personalItem = await screen.findByRole("menuitemradio", {
      name: "Personal",
    });
    const atlomyItem = screen.getByRole("menuitemradio", { name: "Atlomy" });
    expect(personalItem).toHaveAttribute("data-state", "checked");
    expect(atlomyItem).toHaveAttribute("data-state", "unchecked");
    expect(
      screen.getByText("Default for new sessions: Personal"),
    ).toBeInTheDocument();

    await user.click(atlomyItem);
    expect(setSpy).not.toHaveBeenCalled();
    expect(
      await screen.findByText(
        "Switch to Atlomy? The session restarts its agent and keeps the conversation",
      ),
    ).toBeInTheDocument();

    await user.click(screen.getByTestId("session-account-confirm"));
    await waitFor(() => expect(setSpy).toHaveBeenCalledWith(SLOT, "atlomy"));
    await waitFor(() =>
      expect(
        screen.getByRole("menuitemradio", { name: "Atlomy" }),
      ).toHaveAttribute("data-state", "checked"),
    );
    expect(await screen.findByText("Switched to Atlomy")).toBeInTheDocument();
  });

  it("shows deferred toast when switching during an active turn", async () => {
    const user = userEvent.setup();
    vi.spyOn(api, "setChatSlotAccount").mockResolvedValue({
      ...ELIGIBLE_STATE,
      ok: true,
      account: "atlomy",
      current: "atlomy",
      deferred: true,
    } satisfies SlotAccountSwitchResult);

    renderSubmenu("dropdown");
    const subTrigger = await screen.findByTestId("session-account-submenu");
    await act(async () => {
      subTrigger.focus();
      fireEvent.keyDown(subTrigger, { key: "ArrowRight" });
    });
    await user.click(
      await screen.findByRole("menuitemradio", { name: "Atlomy" }),
    );
    await user.click(await screen.findByTestId("session-account-confirm"));

    expect(
      await screen.findByText("Will switch after the current turn"),
    ).toBeInTheDocument();
  });

  it("refetches eligibility when the session switches agent, keeping the stored account", async () => {
    const CODEX_STATE: SlotAccountState = {
      ...ELIGIBLE_STATE,
      eligible: false,
      current: "atlomy",
      reason: "Applies to Claude sessions; this session is on Codex ACP",
    };
    store.dispatch(sseSlots([{ key: SLOT, agent: "derrick" }] as never));
    const spy = vi
      .spyOn(api, "chatSlotAccount")
      .mockResolvedValueOnce({ ...ELIGIBLE_STATE, current: "atlomy" })
      .mockResolvedValueOnce(CODEX_STATE);
    renderSubmenu();
    await waitFor(() => expect(spy).toHaveBeenCalledTimes(1));
    expect(screen.queryByTestId("session-account-disabled")).toBeNull();

    act(() => {
      store.dispatch(updateSlot({ key: SLOT, agent: "crew-codex" }));
    });
    await waitFor(() => expect(spy).toHaveBeenCalledTimes(2));
    const disabled = await screen.findByTestId("session-account-disabled");
    expect(disabled.textContent).toContain("this session is on Codex ACP");
  });
});
