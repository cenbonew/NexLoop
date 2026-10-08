"""CLI labels through actual authenticated PostgreSQL Conversation HTTP reads."""
import sys
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from test_web_chat_http import actual_chat, conversations, browser_business, published_action, uow, identity, browser, login, ORIGIN, create_actual

import nexloop_eios.http_api as module

@pytest.mark.parametrize('profile',[None,'deterministic-test','real-provider','disabled'])
def test_cli_label_actual_pg_http_without_readiness_claim(actual_chat,monkeypatch,profile):
    client,headers,fixture=actual_chat
    created=create_actual(client,headers)
    # Reuse only the genuine fixture's private configuration, not its port/session identity.
    config=client.app.state.backend
    captured=[]
    original_create=module.create_app
    def capture(configuration):
        captured.append(configuration)
        return original_create(configuration)
    monkeypatch.setattr(module,'create_app',capture)
    import uvicorn
    monkeypatch.setattr(uvicorn,'run',lambda *args,**kwargs:None)
    argv=['api','--mode','test','--database-url-file','unused-private-dsn','--signing-key-file','unused-private-key','--artifact-root','unused-root','--signing-key-id','explicit']
    if profile is not None:argv+=['--execution-profile',profile]
    monkeypatch.setattr(sys,'argv',argv);assert module.main()==0
    assert captured[0].execution_profile==profile
    # API label roundtrip is checked on the real PG/browser app, by changing only
    # deployment metadata at construction (never a business object or auth token).
    from nexloop_eios.web_chat_http import router
    from nexloop_eios.browser_http import router as auth_router
    from fastapi import FastAPI
    from nexloop_eios.browser_http import BrowserConfiguration
    boundary=FastAPI();boundary.state.browser_store=client.app.state.browser_store;boundary.state.browser_rate_key=client.app.state.browser_rate_key
    browser_config=BrowserConfiguration(Path('unused'),Path('unused'),'synthetic-a','synthetic-browser-app',ORIGIN)
    boundary.include_router(auth_router(browser_config))
    boundary.include_router(router(browser_config,ports_for_browser=lambda request,session:config.authenticate_browser(session),execution_profile=profile))
    with TestClient(boundary,base_url=ORIGIN) as actual:
        logged=login(actual,fixture['base']['identity']);assert logged.status_code==200
        response=actual.get('/api/v1/conversations');assert response.status_code==200
        row=next(item for item in response.json()['items'] if item['id']==created['id'])
        if profile is None:assert 'execution_profile' not in row
        else:assert row['execution_profile']==profile
    ready=client.get('/health/ready');assert ready.status_code==503 and ready.json()['ready'] is False


def test_cli_rejects_invalid_profile_before_server(monkeypatch):
    monkeypatch.setattr(sys,'argv',['api','--mode','test','--database-url-file','unused','--signing-key-file','unused','--artifact-root','unused','--signing-key-id','explicit','--execution-profile','invented'])
    with pytest.raises(SystemExit) as error:module.main()
    assert error.value.code==2
