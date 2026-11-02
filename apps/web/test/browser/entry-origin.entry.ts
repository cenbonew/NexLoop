// S81 R67: the real enterWithTicket, bundled alone for tests/test_entry_origin_real_browser.py (no React render).
import {enterWithTicket} from '../../src/workbench/pages/Entries';
(window as unknown as {enter:()=>void}).enter=()=>{enterWithTicket('webchat').catch(()=>{document.title='failed';});};
