// AT-015 end-to-end DOM test (node environment; the test builds its own jsdom window so undici keeps Node's Event classes): not part of `vitest run apps/web/test` (it needs a live API); run by tests/test_sse_dom_tcp_resume.py.
export default {test:{root:import.meta.dirname,include:['*.e2e.ts'],environment:'node',testTimeout:60000}};
