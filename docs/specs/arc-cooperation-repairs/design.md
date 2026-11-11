# Design

Use a compact prefix renderer alongside the full `arc_brief` response. Reserve
space for unseen entries and fixed verification text. Summarize overflow with
counts and full-brief/board pointers. Divide the total cap between member arcs.

Add optional CLAIM `work_type` (implementation default, review explicit).
PR bodies use `Arc-Claim: <entry id>` or `Arc-Step: <exact step>` beside the arc
tag. Resolve unique implementation claims within the arc; step matching also
requires the branch's lane. Store the linked claim on the GitHub receipt.

Add FACT `resolution` (completed, superseded, abandoned), with reply_to targeting
a CLAIM and text giving the reason. Validate owner/coordinator authority under
the store lock. The shared closure reducer feeds open claims and collision counts.
This does not advance milestones or assert review acceptance.

Test bounded oversized and multi-arc prefixes, cursor progress, explicit merge
linking, ambiguous/review rejection, identity and authorization through routes.
