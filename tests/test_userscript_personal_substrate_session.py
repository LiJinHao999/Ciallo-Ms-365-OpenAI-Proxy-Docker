from __future__ import annotations

import json
import subprocess

import pytest

from test_userscript_consumer_email import SCRIPT, _email_resolution_source
from test_userscript_substrate_email import records


def _source(start, end):
    return SCRIPT[SCRIPT.index(start):SCRIPT.index(end)]


def _execute(body, *, storage=None, session=None, origin="https://copilot.com"):
    program = f"""
const values={json.dumps(records() if storage is None else storage)};
const tabValues={json.dumps(session or {})};
const storage=values=>({{get length(){{return Object.keys(values).length}},key(i){{return Object.keys(values)[i]}},getItem(k){{return values[k]??null}}}});
const localStorage=storage(values),sessionStorage=storage(tabValues);
const location={{href:{json.dumps(origin + '/chats')}}};
const tr=key=>key;
{_email_resolution_source()}
{_source('    // Export MSAL localStorage', '    // Capture the CURRENT chat conversation URL')}
{body}
"""
    result = subprocess.run(
        ["node", "-"], input=program, capture_output=True, text=True, check=True,
        encoding="utf-8", timeout=20,
    )
    return json.loads(result.stdout)


@pytest.mark.parametrize("change", [
    "values['msal.active-account-filters']=JSON.stringify({homeAccountId:'two'});",
    "tabValues['msal.active-account-filters']=JSON.stringify({homeAccountId:'two'});",
    "values['msal.active-account-filters']=JSON.stringify({homeAccountId:'two'});delete values['cached-token'];captureSubstrateIdentity(token);",
])
def test_identity_change_while_cookie_collection_is_pending_never_sends(change):
    body = f"""
const token='header.key.iv.cipher.tag';
const getUserApiKey=()=> 'key';
const hasGMCookie=()=>true;
const pushActivation={{m365:false}};
let requests=0,release;
const getAllCookies=()=>new Promise(resolve=>{{release=resolve}});
const gmFetch=async()=>{{requests++;return {{json:async()=>({{}})}}}};
{_source('    async function pushUserToken(', '    async function pushUserCookies(')}
captureSubstrateIdentity(token);
(async()=>{{
  const pending=pushUserToken('https://proxy.invalid',token).then(()=>null,e=>e.message);
  {change}
  release([{{name:'__Host-session',value:'secret',domain:'copilot.com',path:'/',secure:true}}]);
  const error=await pending;
  console.log(JSON.stringify({{requests,error}}));
}})().catch(e=>{{console.error(e);process.exitCode=1}});
"""
    assert _execute(body) == {"requests": 0, "error": "no_substrate_identity"}


@pytest.mark.parametrize("expiry", ["0", "-1", "NaN", "Infinity", "", True, None])
def test_invalid_issuer_expiry_remains_unknown(expiry):
    storage = records()
    record = json.loads(storage["cached-token"])
    record["expiresOn"] = expiry
    storage["cached-token"] = json.dumps(record)
    assert _execute("console.log(JSON.stringify(getPersonalSubstrateExpiry('header.key.iv.cipher.tag','home:one')));", storage=storage) == 0


def test_expiry_requires_the_exact_jwe_and_canonical_account_not_another_audience():
    storage = records()
    record = json.loads(storage["cached-token"])
    record["expiresOn"] = "1900000000"
    storage["cached-token"] = json.dumps(record)
    storage["graph-token"] = json.dumps({
        **record, "secret": "different-token", "target": "https://graph.microsoft.com", "expiresOn": "2000000000",
    })
    body = """
const token='header.key.iv.cipher.tag';
console.log(JSON.stringify([
  getPersonalSubstrateExpiry(token,'home:one'),
  getPersonalSubstrateExpiry(token,'home:two'),
  getPersonalSubstrateExpiry('unknown.key.iv.cipher.tag','home:one')
]));
"""
    assert _execute(body, storage=storage) == [1900000000, 0, 0]


def test_conflicting_expiry_for_the_same_jwe_is_not_guessed():
    storage = records()
    record = json.loads(storage["cached-token"])
    storage["cached-token"] = json.dumps({**record, "expiresOn": "1900000000"})
    session = {"tab-token": json.dumps({**record, "expiresOn": "1900000300"})}
    assert _execute("console.log(JSON.stringify(getPersonalSubstrateExpiry('header.key.iv.cipher.tag','home:one')));", storage=storage, session=session) == 0


def test_encrypted_jwe_segments_never_supply_an_issuer_expiry():
    import base64

    encoded = base64.urlsafe_b64encode(b'{"exp":2000000000}').decode().rstrip("=")
    token = f"header.{encoded}.iv.cipher.tag"
    body = f"console.log(JSON.stringify(getPersonalSubstrateExpiry({json.dumps(token)},'home:one')));"
    assert _execute(body, storage={}) == 0


@pytest.mark.parametrize("available,cookies,error", [
    (False, [], "gm_unavailable_alert"),
    (True, [], "no_cookies"),
    (True, [{"name": "unrelated", "value": "secret", "domain": "evil.invalid", "path": "/"}], "no_cookies"),
])
def test_unreusable_personal_session_is_rejected_before_any_request(available, cookies, error):
    body = f"""
const token='header.key.iv.cipher.tag';
const getUserApiKey=()=> 'key';
const hasGMCookie=()=>{str(available).lower()};
const pushActivation={{m365:false}};
const getAllCookies=async()=>{json.dumps(cookies)};
let requests=0;
const gmFetch=async()=>{{requests++;return {{json:async()=>({{}})}}}};
{_source('    async function pushUserToken(', '    async function pushUserCookies(')}
captureSubstrateIdentity(token);
(async()=>{{
  const error=await pushUserToken('https://proxy.invalid',token).then(()=>null,e=>e.message);
  console.log(JSON.stringify({{requests,error}}));
}})().catch(e=>{{console.error(e);process.exitCode=1}});
"""
    assert _execute(body) == {"requests": 0, "error": error}


def test_cookie_scope_rejects_suffix_spoofing_expired_and_empty_credentials():
    body = """
const cookies=[
  {name:'valid',value:'secret',domain:'.login.live.com',path:'/',secure:true,httpOnly:true,sameSite:'None',expires:1900000000},
  {name:'spoof',value:'secret',domain:'copilot.com.evil.invalid',path:'/'},
  {name:'unrelated',value:'secret',domain:'.bing.com',path:'/'},
  {name:'expired',value:'secret',domain:'copilot.com',path:'/',expires:1},
  {name:'empty',value:'',domain:'copilot.com',path:'/'}
];
console.log(JSON.stringify(getPersonalSubstrateCookies(cookies,1800000000)));
"""
    assert _execute(body) == [{
        "name": "valid", "value": "secret", "domain": ".login.live.com", "path": "/",
        "secure": True, "httpOnly": True, "sameSite": "None", "expires": 1900000000,
    }]


def test_storage_snapshot_preserves_opaque_msal_strings_without_exporting_chat_data():
    storage = {
        "msal.account.keys": '["opaque-key"]',
        "opaque-key": '{"id":"encrypted","data":"opaque"}',
        "msal.cache.encryption": "raw opaque string",
        "chat-history": "private conversation",
    }
    assert _execute("console.log(JSON.stringify(getMsalStorage(localStorage)));", storage=storage) == {
        key: value for key, value in storage.items() if key != "chat-history"
    }


def test_cookie_collection_keeps_distinct_paths_and_browser_same_site_metadata():
    program = f"""
const GM_cookie={{list(details,callback){{callback([
  {{name:'session',value:'root',domain:'copilot.com',path:'/',sameSite:'no_restriction',httpOnly:true,expirationDate:1900000000}},
  {{name:'session',value:'login',domain:'copilot.com',path:'/login',sameSite:'strict',secure:true}},
  {{name:'other',value:'default',domain:'copilot.com',path:'/',sameSite:'unspecified'}}
]);}}}};
const console={{log(){{}}}};
{_source('    async function getAllCookies(', '    // Export MSAL localStorage')}
getAllCookies().then(cookies=>process.stdout.write(JSON.stringify(cookies)));
"""
    result = subprocess.run(
        ["node", "-"], input=program, capture_output=True, text=True, check=True,
        encoding="utf-8", timeout=20,
    )
    cookies = json.loads(result.stdout)
    assert [(cookie["name"], cookie["path"], cookie["sameSite"]) for cookie in cookies] == [
        ("session", "/", "None"), ("session", "/login", "Strict"), ("other", "/", "None"),
    ]
    assert cookies[0]["expires"] == 1900000000
    assert cookies[0]["httpOnly"] is True
    assert cookies[1]["secure"] is True


def test_observed_account_switch_cannot_be_hidden_by_switching_back_before_collection_finishes():
    body = f"""
const token='header.key.iv.cipher.tag';
const getUserApiKey=()=> 'key';
const hasGMCookie=()=>true;
const pushActivation={{m365:false}};
const callbacks=[];
const GM_cookie={{list(details,callback){{callbacks.push(callback)}}}};
let requests=0;
const gmFetch=async()=>{{requests++;return {{json:async()=>({{}})}}}};
{_source('    async function getAllCookies(', '    // Export MSAL localStorage')}
{_source('    async function pushUserToken(', '    async function pushUserCookies(')}
captureSubstrateIdentity(token);
(async()=>{{
  const pending=pushUserToken('https://proxy.invalid',token).then(()=>null,e=>e.message);
  values['msal.active-account-filters']=JSON.stringify({{homeAccountId:'two'}});
  callbacks.shift()([{{name:'session',value:'secret',domain:'copilot.com',path:'/'}}]);
  values['msal.active-account-filters']=JSON.stringify({{homeAccountId:'one'}});
  for(const callback of callbacks) callback([]);
  const error=await pending;
  console.log(JSON.stringify({{requests,error}}));
}})().catch(e=>{{console.error(e);process.exitCode=1}});
"""
    assert _execute(body) == {"requests": 0, "error": "no_substrate_identity"}


@pytest.mark.parametrize("action", ["pushCookies", "oneClickSetup"])
@pytest.mark.parametrize("origin", ["https://copilot.com", "https://m365.cloud.microsoft"])
def test_personal_cookie_buttons_never_send_credentials_to_enterprise_injection(action, origin):
    body = f"""
const token='header.key.iv.cipher.tag';
const latestToken=token,latestMediaAuth=null,latestDesignerAuth=null,latestRefreshToken='';
const IS_M365_SITE=false;
const getUserApiKey=()=> 'key',getProxyBase=()=> 'https://proxy.invalid';
const hasGMCookie=()=>true,getUsername=()=>'',getCurrentChatUrl=()=>'';
const pushActivation={{m365:true}};
const getAllCookies=async()=>[{{name:'WLSSC',value:'secret',domain:'.live.com',path:'/'}}];
const document={{getElementById:()=>({{disabled:false,textContent:''}})}};
const alert=()=>{{}};
const mediaAuthSkippedStatus=()=>'',pushMediaAuthWithStatus=async()=>'';
const requests=[];
const gmFetch=async(url,options)=>{{requests.push({{path:new URL(url).pathname,body:JSON.parse(options.body)}});return {{ok:true,json:async()=>({{status:'ok',token_status:{{seconds_remaining:3600}},injected:1,total:1}})}}}};
{_source('    async function pushUserToken(', '    async function pushUserConsumer(')}
{_source('    async function pushCookies(', '    async function pushConsumer(')}
{_source('    async function oneClickSetup(', '    async function pushPayload(')}
captureSubstrateIdentity(token);
(async()=>{{await {action}();console.log(JSON.stringify(requests));}})().catch(e=>{{console.error(e);process.exitCode=1}});
"""
    requests = _execute(body, origin=origin)
    assert [request["path"] for request in requests] == ["/user/account/token"]
    if origin == "https://copilot.com":
        assert requests[0]["body"]["personal_substrate_session"]["origin"] == origin
        assert requests[0]["body"]["personal_substrate_session"]["account_id"] == "home:one"
    else:
        assert "personal_substrate_session" not in requests[0]["body"]
