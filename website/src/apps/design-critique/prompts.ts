import type { Report, ReviewBrief, Screen } from "./types";

const CRITIC =
  "You are an experienced designer running a heuristic design critique — a fellow designer " +
  "looking over someone’s work, not a title on a review panel.\n\n" +
  "Voice: lead with a one-line overall read and a health tally using the NN/g severity names " +
  "(Cosmetic / Minor / Major / Catastrophe), then what is working, then the top 3-5 things " +
  "you would tighten (element → problem → fix), then one line on what the evidence could not " +
  "show. Positives before fixes. No composite 0-100 score. Be warm, specific and concrete. " +
  "Do not invent personas or backstories to justify a finding. Never judge what the supplied " +
  "evidence cannot reveal, and never critique visuals from unrendered source.\n\n";

export const SCHEMA = (multi: boolean): string =>
  "Return ONLY JSON (no prose, no code fences) matching exactly:\n" +
  '{"overallRead":string,"health":string,' +
  '"tally":{"catastrophe":int,"major":int,"minor":int,"cosmetic":int},' +
  '"screens":[{"step":int,"label":string,"path":string}],' +
  '"findings":[{"severity":"cosmetic|minor|major|catastrophe","title":string,' +
  '"category":string,"scope":"screen|flow","steps":[int],"location":string,' +
  '"evidence":string,"fix":string,"rules":[string],' +
  '"box":{"x":number,"y":number,"w":number,"h":number}}],' +
  '"keep":[string],"couldNotSee":[string]}\n\n' +
  '"screens" lists every screen you actually saw, in order, with the absolute image path ' +
  'for each. "box" is the APPROXIMATE region of the issue within ITS screen as fractions 0-1 ' +
  "(x,y = top-left, w,h = size); use null if you cannot localize it.\n\n" +
  (multi
    ? 'Set "scope" to "screen" for a finding that lives on one screen (put that one step in ' +
      '"steps", and give a box). Set "scope" to "flow" for a problem that only exists because ' +
      "this is a sequence — inconsistency between steps, no progress indicator, no way back, " +
      'repeated asks, dead ends. List every step it involves in "steps" and use null for "box". ' +
      "Count a cross-screen problem once, not once per screen.\n\n"
    : 'Use "scope":"screen" and "steps":[1] for every finding.\n\n') +
  'In "rules", name the 1-3 design principles or heuristics the finding rests on ' +
  "(e.g. “Nielsen: consistency”, “Gestalt: proximity”, “WCAG 1.4.3 contrast”).\n\n" +
  'Phrase "fix" as a suggestion (“Consider…”, “One option…”, “You might…”), not a command, ' +
  "for every finding EXCEPT accessibility — accessibility fixes may be stated directly.\n\n" +
  "Only include findings for what you actually saw. List anything you could not see under " +
  '"couldNotSee" instead of guessing.';

export const reviewBriefContext = (brief?: ReviewBrief | string): string => {
  if (!brief) return "";
  if (typeof brief === "string") return "Review target: " + brief;
  const values = [
    brief.projectName,
    brief.repository,
    brief.contextPaths,
    brief.notes,
    brief.targets,
  ];
  if (!values.some((value) => value.trim())) return "";
  const intent = {
    ground:
      "Ground the critique in the current repository and its established product context. Do not invent a replacement direction.",
    reference:
      "Use the repository context as a reference to evaluate consistency, then make bounded improvement suggestions.",
    invent:
      "The repository context is background only. Explore a new direction where the evidence supports it.",
  }[brief.intent];
  return [
    "Review brief — this narrows the run; it does not redefine the whole project:",
    brief.projectName ? "Project: " + brief.projectName : "",
    brief.repository ? "Repository: " + brief.repository : "",
    brief.contextPaths
      ? "Read these supporting files first when they exist (one per line):\n" +
        brief.contextPaths
      : "",
    brief.notes ? "Constraints and context: " + brief.notes : "",
    brief.targets ? "Review target: " + brief.targets : "",
    intent,
    "Do not infer requirements for unselected areas from the project context. State when the supplied target or evidence cannot support a conclusion.",
  ]
    .filter(Boolean)
    .join("\n");
};

export const IMAGES_PROMPT = (
  paths: string[],
  brief?: ReviewBrief | string,
  method?: string,
  couldNotRender?: string[],
): string => {
  const multi = paths.length > 1;
  return (
    CRITIC +
    (method
      ? "Follow this critique method exactly:\n\n" + method + "\n\n"
      : "") +
    (multi
      ? "Please critique this flow of " +
        paths.length +
        " screens, in the order given. " +
        "Run your design-critique skill in FLOW MODE: walk each step in order (what is this " +
        "screen asking the user to do, is the next action obvious, what happens between this " +
        "screen and the next, where is the friction), then check the jumps between steps. " +
        "Do not narrate what each screen contains."
      : "Please run a design critique on this screenshot.") +
    (reviewBriefContext(brief) ? "\n\n" + reviewBriefContext(brief) : "") +
    "\n\n" +
    (couldNotRender && couldNotRender.length
      ? "These screens could not be rendered — list them under couldNotSee: " +
        couldNotRender.join(", ") +
        ".\n\n"
      : "") +
    SCHEMA(multi) +
    "\n\n" +
    "The screens, in order:\n" +
    paths
      .map(
        (p, i) =>
          (multi ? "Step " + (i + 1) + ":\n" : "") + "![screen](" + p + ")",
      )
      .join("\n\n") +
    "\n\n" +
    'For "screens", use these exact paths in this order: ' +
    JSON.stringify(paths) +
    ". " +
    'Give each a label of ONE or TWO plain words naming the screen (e.g. "Cart", "Shipping", ' +
    '"Payment", "Confirmation"). No parentheses, no state descriptions, max 18 characters.'
  );
};

export const ASK_CONTEXT = (rep: Report, screens: Screen[]): string =>
  "Context — you already produced this critique. Do NOT re-critique anything; the user " +
  "just wants to understand parts of it.\n\n" +
  "Overall read: " +
  (rep.overallRead || "") +
  "\n" +
  "Screens: " +
  (screens || [])
    .map((s, i) => i + 1 + ". " + (s.label || "Screen"))
    .join(", ") +
  "\n\n" +
  "Findings:\n" +
  (rep.findings || [])
    .map(
      (f, i) =>
        i +
        1 +
        ". [" +
        f.severity +
        "] " +
        f.title +
        (f.evidence ? " — evidence: " + f.evidence : "") +
        (f.fix ? " — suggested: " + f.fix : "") +
        (f.rules && f.rules.length ? " — based on: " + f.rules.join("; ") : ""),
    )
    .join("\n") +
  "\n\n" +
  'Reply "ready" and nothing else.';

export const ASK_PROMPT = (quote: string, question?: string): string =>
  "The user highlighted this from your critique:\n\n“" +
  quote +
  "”\n\n" +
  "Their question: " +
  (question || "What does this mean?") +
  "\n\n" +
  "Answer in 2-4 plain sentences, as a designer explaining to another designer. Explain the " +
  "reasoning or the principle behind it and what they would actually change. No headings, no " +
  "bullet lists, no restating the question. If the honest answer is that you are not sure or the " +
  "evidence did not show it, say that plainly.";
