# Local release verification

Verified on 31 October 2026 on Windows with Node 24 and Python 3.12.

- 25 JavaScript modules passed syntax checks.
- All 12 web/security/transport tests and all 223 isolated engine tests passed.
- Live integration checks verified 81 OpenAPI operations, 22 read endpoints and 25 media previews.
- An isolated Chromium run opened all nine workspace pages without account or plan UI, checked the profile/settings menus, verified retired signup/signin/pricing redirects, and confirmed no requests to the removed account service.
- The New and Settings pages fit 320 px, 390 px and 820 px browser viewports without horizontal overflow. No page-script errors were reported.
- The web and engine servers remained on loopback. Existing private workspace data was preserved. Tests did not submit real downloads, creator changes, payments, messages or credentials to external services.
- Supabase authentication, paid plans, subscription quotas, customer workers, payment code and hosting/tunnel scaffolding were removed from the distributable source. Optional personal Telegram and MEGA integrations remain.
- The release excludes private environments, cookies, databases, media, generated browsers/tools and captured reference assets. Inter font licenses remain included.

The CI workflow runs the checks on Windows, Ubuntu and macOS after publication. Local results above do not claim those remote jobs have completed. Real platform access still depends on each user's session, permissions and platform availability.
