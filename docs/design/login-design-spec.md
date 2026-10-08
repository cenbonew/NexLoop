# Login UI selected concept

Selected self-authored concept (not a claim of user design approval): nexloop-login-concept.png, builtin image_gen, native1586x992. Prompt: complete restrained enterprise NexLoop Chinese login, true white, navy12233b, muted66758b, borderE2E8F0, blue2463eb; text-only wordmark; open form, no outer card, images, icons, gradients or fabricated metrics. Exact allowed initial copy: NexLoop / 登录 NexLoop / 使用工作账户继续 / 账号 / 输入账号 / 密码 / 输入密码 / 登录 / 会话将在本设备保持，退出后立即失效。 / NexLoop · 持续运营.

Implementation inventory: system sans, desktop wordmark32px, heading44/55px, subtitle18/27px, labels17/24px, inputs/button64px, form524px with44px optical right offset, header/footer90px horizontal margin. White canvas, pale bordered radius10 input, blue radius9 button, navy hierarchy and muted captions. Mobile390px:24px gutters,32px heading,52px controls, no horizontal transform. No raster imagery in production UI; concept is design evidence only.

Components: App session query/state composition, Login accessible real form, Account current session/logout, API typed validators, CSS tokens. React19/Vite/TS/TanStack Query as handed off. No mock business data or inert navigation. Password remains transient form state and is cleared on success; session cookie stays HttpOnly; no localStorage, sessionStorage or query persistence. Every write has retry:false.

Authenticated/loading/error states extend the same open layout. Exact failure/status copy is functional and intentionally absent from the initial concept; initial login copy may not drift. Current workspace/availability comes from the actual API. React components must not claim unavailable Host/operations are working. Browser QA and concept/render ledger remain required before completion.
