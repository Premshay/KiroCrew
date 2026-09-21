import { afterEach, beforeEach, expect, it, vi } from "vitest";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { Provider } from "react-redux";
import { http, HttpResponse } from "msw";
import { server } from "../../../integration/mocks/server";
import { createTestStore } from "../../test/helpers";
import SetupPanel from "./SetupPanel";

const API = "/api/apps/auto-improvement";
const config = {
  clone: "/clones/example",
  target_url: "https://github.com/example/repo",
  branch: "main",
};
let client: QueryClient;
let run: { status: string; activity?: { note: string }[]; error?: string };
let requests: unknown[];

function mount(value: Record<string, unknown> = config) {
  client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const store = createTestStore();
  const tree = (next: Record<string, unknown>) => (
    <Provider store={store}>
      <MemoryRouter>
        <QueryClientProvider client={client}>
          <SetupPanel config={next} />
        </QueryClientProvider>
      </MemoryRouter>
    </Provider>
  );
  const view = render(tree(value));
  return {
    setConfig: (next: Record<string, unknown>) => view.rerender(tree(next)),
  };
}
const calibrate = () => screen.getByRole("button", { name: "Calibrate" });
const check = () => screen.getByRole("button", { name: "Check environment" });
async function ready() {
  await waitFor(() => expect(check()).toBeEnabled());
  fireEvent.click(check());
  await waitFor(() => expect(calibrate()).toBeEnabled());
}
beforeEach(() => {
  run = { status: "idle" };
  requests = [];
  server.use(
    http.get(`${API}/branches`, () => HttpResponse.json({ branches: [] })),
    http.get(`${API}/run`, () => HttpResponse.json(run)),
    http.post(`${API}/environment/check`, () =>
      HttpResponse.json({ ok: true }),
    ),
    http.put(`${API}/config`, () => HttpResponse.json({ ok: true })),
    http.post(`${API}/calibrate`, async ({ request }) => {
      requests.push(await request.json());
      run = {
        status: "calibrating",
        activity: [{ note: "Baseline sample 1 of 3" }],
      };
      return HttpResponse.json({ status: "calibrating", run_id: "cal-test" });
    }),
    http.post(`${API}/run/stop`, () => {
      run = { ...run, status: "stopping" };
      return HttpResponse.json(run);
    }),
  );
});
afterEach(() => {
  client?.clear();
  vi.restoreAllMocks();
});

it("requires a saved, checked environment and describes workload cost without an estimate", async () => {
  mount();
  await waitFor(() => expect(check()).toBeEnabled());
  expect(calibrate()).toBeDisabled();
  expect(calibrate()).toHaveAccessibleDescription(
    /repeats execute the configured workload.*Total duration is unknown until measured/,
  );
  fireEvent.click(calibrate());
  expect(requests).toEqual([]);
  await ready();
  fireEvent.change(screen.getByLabelText(/Nonsecret variables/), {
    target: { value: "MODE=test" },
  });
  expect(calibrate()).toBeDisabled();
  fireEvent.click(check());
  await screen.findByText("Ready");
  expect(calibrate()).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Save environment" }));
  await screen.findByText("Unchecked");
  expect(calibrate()).toBeDisabled();
  await ready();
});

it("posts once, displays calibration progress, refreshes the ruler and stops through the existing flow", async () => {
  mount();
  await ready();
  const invalidate = vi.spyOn(client, "invalidateQueries");
  fireEvent.click(calibrate());
  await screen.findByText("Calibrating…");
  expect(requests).toEqual([{}]);
  expect(screen.getByRole("log")).toHaveTextContent("Baseline sample 1 of 3");
  expect(calibrate()).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Stop run" }));
  await screen.findByText("Stopping…");
  expect(screen.getByRole("button", { name: "Stop run" })).toBeDisabled();
  invalidate.mockClear();
  run = { status: "idle" };
  await act(async () => {
    await client.invalidateQueries({ queryKey: ["auto-improvement-run"] });
  });
  await waitFor(() =>
    expect(invalidate).toHaveBeenCalledWith({
      queryKey: ["auto-improvement-ruler"],
    }),
  );
  expect(calibrate()).toBeDisabled();
});

it.each(["running", "calibrating", "stopping"])(
  "blocks calibration while %s",
  async (status) => {
    run = { status };
    mount();
    await screen.findByRole("button", { name: "Stop run" });
    expect(calibrate()).toBeDisabled();
    expect(check()).toBeDisabled();
    expect(requests).toEqual([]);
  },
);

it.each([
  [409, { error: "A run is already active" }, "A run is already active"],
  [200, { ok: false, error: "Calibration refused" }, "Calibration refused"],
])("surfaces rejected calibration (%s)", async (status, body, message) => {
  server.use(
    http.post(`${API}/calibrate`, () =>
      HttpResponse.json(body, { status: status as number }),
    ),
  );
  mount();
  await ready();
  fireEvent.click(calibrate());
  await waitFor(() =>
    expect(screen.getByRole("alert")).toHaveTextContent(message as string),
  );
  expect(screen.queryByText("Calibrating…")).not.toBeInTheDocument();
});

it("surfaces a network failure", async () => {
  server.use(http.post(`${API}/calibrate`, () => HttpResponse.error()));
  mount();
  await ready();
  fireEvent.click(calibrate());
  await waitFor(() =>
    expect(screen.getByRole("alert")).toHaveTextContent(/fetch/i),
  );
});

it("locks other actions while calibration is being submitted", async () => {
  let resolve!: () => void;
  const pending = new Promise<void>((done) => {
    resolve = done;
  });
  server.use(
    http.post(`${API}/calibrate`, async () => {
      await pending;
      return HttpResponse.json({ status: "calibrating", run_id: "cal-test" });
    }),
  );
  mount();
  await ready();
  fireEvent.click(calibrate());
  await waitFor(() => expect(calibrate()).toBeDisabled());
  expect(check()).toBeDisabled();
  expect(screen.getByRole("button", { name: /start|run/i })).toBeDisabled();
  await act(async () => resolve());
});

it("invalidates readiness when the branch changes", async () => {
  const view = mount();
  await ready();
  view.setConfig({ ...config, branch: "feature" });
  expect(calibrate()).toBeDisabled();
  expect(requests).toEqual([]);
});

it("does not offer calibration before a repository is configured", async () => {
  mount({});
  await waitFor(() =>
    expect(client.getQueryData(["auto-improvement-run"])).toEqual({
      status: "idle",
    }),
  );
  expect(
    screen.queryByRole("button", { name: "Calibrate" }),
  ).not.toBeInTheDocument();
});

it("blocks calibration when the environment check fails", async () => {
  server.use(
    http.post(`${API}/environment/check`, () =>
      HttpResponse.json({ ok: false, error: "Environment unavailable" }),
    ),
  );
  mount();
  await waitFor(() => expect(check()).toBeEnabled());
  fireEvent.click(check());
  await screen.findByText("Environment unavailable");
  expect(calibrate()).toBeDisabled();
  expect(requests).toEqual([]);
});

it("blocks calibration when current run status cannot be read", async () => {
  server.use(
    http.get(`${API}/run`, () =>
      HttpResponse.json({ error: "Run status unavailable" }, { status: 503 }),
    ),
  );
  mount();
  await screen.findByText("Run status unavailable");
  expect(calibrate()).toBeDisabled();
  expect(check()).toBeDisabled();
  expect(requests).toEqual([]);
});

it("shows a failed calibration from the polled run as an error", async () => {
  run = { status: "error", error: "Calibration workload failed" };
  mount();
  await waitFor(() =>
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Calibration workload failed",
    ),
  );
});

it("saves measurement settings and resets them when switching repositories", async () => {
  let payload: Record<string, unknown> | undefined;
  server.use(
    http.put(`${API}/config`, async ({ request }) => {
      payload = (await request.json()) as Record<string, unknown>;
      return HttpResponse.json({ config: payload });
    }),
  );
  const view = mount();
  await ready();
  fireEvent.change(screen.getByLabelText("Benchmark command (optional)"), {
    target: { value: "python bench.py" },
  });
  fireEvent.change(screen.getByLabelText("Slowed control command"), {
    target: { value: "python bench.py --slow" },
  });
  fireEvent.change(
    screen.getByLabelText("Focused pytest paths (one per line)"),
    { target: { value: "tests/unit\ntests/api" } },
  );
  fireEvent.change(screen.getByLabelText("Full regression timeout (seconds)"), {
    target: { value: "4200" },
  });
  expect(check()).toBeDisabled();
  expect(calibrate()).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Save environment" }));
  await waitFor(() =>
    expect(payload).toMatchObject({
      benchmarkCommand: "python bench.py",
      benchmarkCanaryCommand: "python bench.py --slow",
      focusedTestPaths: ["tests/unit", "tests/api"],
      fullRegressionTimeoutSeconds: 4200,
    }),
  );
  view.setConfig({ ...config, ...payload });
  await waitFor(() => expect(check()).toBeEnabled());
  expect(calibrate()).toBeDisabled();
  view.setConfig({
    clone: "/clones/other",
    target_url: "https://github.com/example/other",
  });
  expect(screen.getByLabelText("Benchmark command (optional)")).toHaveValue("");
  expect(
    screen.getByLabelText("Focused pytest paths (one per line)"),
  ).toHaveValue("");
  expect(
    screen.getByLabelText("Full regression timeout (seconds)"),
  ).toHaveValue(900);
  expect(calibrate()).toBeDisabled();
});
