"""Config flow steps (user, reauth) with a mocked sign-in."""
import sys, asyncio
from types import SimpleNamespace
from unittest import mock
from pathlib import Path
_HERE = Path(__file__).resolve().parent
root = sys.argv[1] if len(sys.argv) > 1 else str(_HERE.parent)
sys.path.insert(0, root)
from custom_components.haier_evo import config_flow as cf
from homeassistant.data_entry_flow import AbortFlow

fails = 0
def check(name, cond, info=""):
    global fails
    fails += not cond
    print(("OK  " if cond else "FAIL"), name, info)


class Entries:
    def __init__(self, entries=()):
        self.entries = list(entries)
        self.updated, self.reloaded = [], []
        self.flow = SimpleNamespace(async_progress_by_handler=lambda *a, **k: [])
    def async_entries(self, *a, **k): return self.entries
    def async_entry_for_domain_unique_id(self, domain, uid):
        return next((e for e in self.entries if e.unique_id == uid), None)
    def async_get_entry(self, eid): return next((e for e in self.entries if e.entry_id == eid), None)
    def async_get_known_entry(self, eid): return self.async_get_entry(eid)
    def async_update_entry(self, entry, **kw):
        self.updated.append(kw)
        if "data" in kw: entry.data = kw["data"]
        return True
    def async_schedule_reload(self, eid): self.reloaded.append(eid)


def make_flow(entries, source="user", entry_id=None):
    flow = cf.ConfigFlow()
    flow.hass = SimpleNamespace(config_entries=entries)
    flow.handler = "haier_evo"
    flow.flow_id = "f1"
    flow.context = {"source": source}
    if entry_id:
        flow.context["entry_id"] = entry_id
    return flow


async def main():
    tokens = {"token": "T", "refreshtoken": "R"}

    # user step: form, then entry without password
    flow = make_flow(Entries())
    form = await flow.async_step_user()
    keys = [str(k) for k in form["data_schema"].schema]
    check("user form: email/password/region", form["type"] == "form" and keys == ["email", "password", "region"], str(keys))
    pw_selector = form["data_schema"].schema[next(k for k in form["data_schema"].schema if str(k) == "password")]
    check("password field is a password selector", getattr(pw_selector, "config", {}).get("type") == "password", str(getattr(pw_selector, "config", None)))
    with mock.patch.object(cf, "sign_in", mock.AsyncMock(return_value=tokens)) as si:
        res = await flow.async_step_user({"email": " User@Example.com ", "password": "pw", "region": "ru"})
    check("user step creates entry with tokens and no password",
          res["type"] == "create_entry" and res["data"] == {"email": "User@Example.com", "region": "ru", "tokens": tokens},
          str(res.get("data")))
    check("user step signs in with the given password", si.await_args.args[1:] == ("User@Example.com", "pw", "ru"))
    check("unique_id is e-mail + region", flow.unique_id == "user@example.com_ru", str(flow.unique_id))

    # wrong password / no connection
    for exc, code in ((cf.InvalidCredentials(), "invalid_auth"), (cf.CannotConnect(), "cannot_connect")):
        flow = make_flow(Entries())
        with mock.patch.object(cf, "sign_in", mock.AsyncMock(side_effect=exc)):
            res = await flow.async_step_user({"email": "a@b.c", "password": "pw", "region": "ru"})
        check(f"user step error {code}, password not echoed back",
              res["type"] == "form" and res["errors"] == {"base": code} and "pw" not in str(res.get("data_schema").schema), str(res.get("errors")))

    # duplicate account
    existing = SimpleNamespace(entry_id="E1", unique_id="a@b.c_ru", data={"email": "a@b.c", "region": "ru"},
                               domain="haier_evo", source="user", title="a@b.c (ru)", update_listeners=[])
    flow = make_flow(Entries([existing]))
    with mock.patch.object(cf, "sign_in", mock.AsyncMock(return_value=tokens)) as si:
        try:
            res = await flow.async_step_user({"email": "A@b.c", "password": "pw", "region": "ru"})
            aborted = res.get("type") == "abort" and res.get("reason") == "already_configured"
        except AbortFlow as e:
            aborted = e.reason == "already_configured"
    check("duplicate account aborts before signing in", aborted and si.await_count == 0)

    # reauth: form shows the e-mail, success updates tokens and reloads
    entries = Entries([existing])
    flow = make_flow(entries, source="reauth", entry_id="E1")
    with mock.patch.object(cf.ConfigFlow, "_get_reauth_entry", lambda self: existing):
        form = await flow.async_step_reauth({})
        check("reauth form: password only, e-mail in the text",
              form["step_id"] == "reauth_confirm" and [str(k) for k in form["data_schema"].schema] == ["password"]
              and form["description_placeholders"].get("email") == "a@b.c")
        with mock.patch.object(cf, "sign_in", mock.AsyncMock(side_effect=cf.InvalidCredentials())):
            res = await flow.async_step_reauth_confirm({"password": "bad"})
        check("reauth wrong password -> invalid_auth", res["errors"] == {"base": "invalid_auth"})
        with mock.patch.object(cf, "sign_in", mock.AsyncMock(return_value=tokens)):
            res = await flow.async_step_reauth_confirm({"password": "good"})
    check("reauth success -> tokens into the entry, reload, abort reauth_successful",
          res["type"] == "abort" and res["reason"] == "reauth_successful"
          and existing.data.get("tokens") == tokens and "password" not in existing.data and entries.reloaded == ["E1"],
          f"{res.get('reason')} {existing.data} {entries.reloaded}")

    # sign_in maps errors
    class C:
        def __init__(self, *a, **k): pass
        def stop(self): pass
    hass = SimpleNamespace(async_add_executor_job=lambda fn: asyncio.sleep(0, result=fn()))
    import requests
    from custom_components.haier_evo import api
    for raise_exc, expect in ((api.AuthUserError("bad"), cf.InvalidCredentials),
                              (api.InvalidAuth(), cf.InvalidCredentials)):
        class Cl(C):
            def login(self): raise raise_exc
        with mock.patch.object(cf.api, "Haier", Cl):
            try:
                await cf.sign_in(hass, "a", "b", "ru"); got = None
            except Exception as e:
                got = e
        check(f"sign_in maps {type(raise_exc).__name__} -> {expect.__name__}", isinstance(got, expect), repr(got))
    class Net(C):
        def login(self):
            try:
                raise requests.exceptions.ConnectTimeout("t")
            except Exception as e:
                raise api.InvalidAuth() from e
    with mock.patch.object(cf.api, "Haier", Net):
        try:
            await cf.sign_in(hass, "a", "b", "ru"); got = None
        except Exception as e:
            got = e
    check("sign_in maps a network error -> CannotConnect", isinstance(got, cf.CannotConnect), repr(got))

asyncio.run(main())
print("FAILS:", fails)
sys.exit(1 if fails else 0)
