import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";

import StopEventCard from "../pages/chat/StopEventCard";
import { renderWithProviders } from "./helpers";

describe("StopEventCard", () => {
  it("does not present a channel interruption as an operator stop or reset", () => {
    renderWithProviders(
      <StopEventCard
        message={{
          role: "system",
          content: "",
          cls: "",
          meta: { state: "stop_failed_reset", source: "channel" },
        }}
      />,
    );

    expect(screen.getByTestId("stop-event-card")).toHaveAttribute(
      "data-source",
      "channel",
    );
    expect(
      screen.getByText("Incoming message interrupted the prior turn"),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("[Stop Failed, Session Reset]"),
    ).not.toBeInTheDocument();
  });
});
