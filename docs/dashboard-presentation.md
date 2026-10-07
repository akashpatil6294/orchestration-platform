# Dashboard presentation update

Implemented in the Downloads checkout on 7 October 2026.

## Files

- `frontend/src/index.css`: warm neutral theme tokens and Google Fonts loading with local fallbacks.
- `frontend/src/dashboard.css`: fixed shell, responsive drawer, square controls, dashboard layout, status dots, accessible focus styles.
- `frontend/src/main.tsx`: imports the presentation stylesheet after the existing base styles.
- `frontend/src/components/AppShell.tsx`: existing navigation and account actions, header placement, avatar, drawer and keyboard dismissal. Alerts and Recovery are now included in navigation using their existing routes.
- `frontend/src/components/DashboardSections.tsx`: AlertBar, SummaryBand, RunsTable, WorkersPanel, MetricsPanel and header/navigation presentation portals.
- `frontend/src/pages/DashboardPage.tsx`: composes presentation sections around the existing queries, preferences and action handlers.
- `frontend/src/components/DashboardSections.test.tsx`: eight presentation and interaction regressions.
- `frontend/src/components/AppShell.test.tsx`: account menu selector uses its accessible name now that the navigation toggle also exposes its expanded state.

## Data and behavior

The existing dashboard query still uses `window_hours` and the existing polling hook. Run lifecycle operations, approvals, demo installation, manual workflow execution, quota requests, worker capacity checks and activity filtering retain their handlers. Recent runs can now be filtered by workflow, ID, version or status; copy uses the full ID, and the trace link opens the existing run route. Additional run actions are in each row's More actions disclosure.

Counts, success rate, average/P95 duration, schedules and retry/failure summaries come from dashboard stats. Worker rows and the 16-segment slot display use actual running steps and capacities, explicitly labeling partial worker lists. The stale alert uses the backend stale flag and heartbeat timestamp. The dead-letter navigation dot is rendered only when the existing dashboard attention response reports a DLQ item; the backend emits that item only when its count is positive. It is not a new global query.

Throughput uses the API's time buckets and started counts, with no synthetic bars or series. Fewer than ten executions shows the requested empty state. The latency breakdown is hidden without duration statistics.

There is no `design/dashboard-reference.html` in this checkout. The implementation follows the supplied written specification. Environment/region, invented role/pool names, worker health percentages, live/sample labels, thresholds, P50/P90/P99/maximum latency and static slot counts were omitted because the existing response does not provide them. Queues remain on Workers & queues; API tokens remain in Settings, matching the actual route table. No backend, API client, authentication, route definitions or data-fetching modules changed.

## Verification

- All 18 dashboard, shell and presentation tests pass.
- Full existing suite: 121 passed and 7 failed in WorkflowDetailPage tests on the run before the final two presentation regressions were added. All failures share the existing missing `/api/v1/teams` fixture (`teams.length` receives undefined). Those test/runtime modules were not changed by this presentation work.
- Lint exits successfully with existing warnings in other modules.
- Vite production bundling succeeds, with its bundle-size advisory.
- The standard `npm run build` is still blocked by existing unused `replayInput` and `setReplayInput` declarations at RunDetailPage.tsx:284. This state was left unchanged under the presentation-only constraint.
- Visually inspected a clearly labeled static test snapshot at 1440, 1024 and 390 pixels. No document-wide horizontal overflow; the runs table scrolls within its panel. These checks validate presentation with fixtures, not authenticated live data.
- The live browser reaches the Google sign-in page. No signed-in browser session was available, so a full live dashboard review was not completed.

## Local preview

From `frontend`, use `npm run dev -- --host 127.0.0.1` and open http://127.0.0.1:5173. Keep the project's existing backend and Google sign-in setup. The preview server was started during this update.

For a temporary static visual snapshot, set `DASHBOARD_VISUAL_SNAPSHOT=1` while running `vitest run src/components/DashboardSections.test.tsx`. It writes `.dashboard-review.html` in the frontend folder for inspection via Vite. This file contains test fixtures, is not part of the application, and should be deleted after review. No snapshot is shipped in the production build.
