# NX-042 T6 — the text layer (default deterministic template)

Scope: design §2, D1, D5, D6; builds on T2 (decision layer) and T3 (port act). Migration `0283_nx042_twin_text.sql` (temporary number, contiguous after 0282 at integration; the dispatcher renumbers at merge). No contract change.

| Item | Implementation |
| --- | --- |
| Renderer | `runtime.nexloop_twin_render_text(generator, base, step, behaviour, kind)`: owner function, no application role executes it. `twin-template/1` (D1 default) picks a fixed template of the event kind (message, refuse_contact, payment_intent; a visit has no text) by the seeded draw `runtime.nexloop_sim_draw(seed\|definition digest\|consumer, 'text:<step>:<behaviour>')`: the same seed and twin version give the same text byte for byte. |
| Templates | `runtime.nexloop_twin_text_templates()`: 8 fixed templates. Every one starts with the marker `【模拟消息】`, names no person, contact or account and quotes no conversation; each passes the T1 real-content guard (`runtime.nexloop_twin_text_looks_real`). |
| Model path | Interface only (D1: an experiment's own approved budget). A listed generator with `uses_model` is refused by the renderer (`model_budget_required`), as bind already refuses it (T3); an unlisted one is `generator_unknown`. Python: `nexloop_eios.twin_port.ModelTextGenerator.render()` always raises `ModelBudgetRequired`. |
| Through act | `authz.nexloop_twin_act` (T3 body, replaced in place, registered in `tests/test_function_copy_guard.py`) takes the text from the renderer instead of T3's fixed placeholder and adds the attributes `text_marker: simulated`, `text_template`, `text_generator` to the event (the S7 port's 16-attribute limit holds). Provenance, replay and the event ceiling are T3's / S7's. |
| Worlds | Only through the port entries: real and test are refused before any lookup (`authz.nexloop_sim_effect_tenant`); the Python port refuses them before any call. |

T3 test touched: `test_nx042_twin_port_pg.py` allows exactly the three text attributes on events that carry text.

Tests: `tests/test_nx042_twin_text_pg.py`.
