"""async_setup_entry / async_remove_entry / background refresh control flow with fakes."""
import sys, asyncio, os, json, tempfile
from types import SimpleNamespace
from unittest import mock
from pathlib import Path
_HERE = Path(__file__).resolve().parent
root = sys.argv[1] if len(sys.argv) > 1 else str(_HERE.parent)
sys.path.insert(0, root)
import custom_components.haier_evo as integ
from custom_components.haier_evo import api
from homeassistant.exceptions import ConfigEntryNotReady, ConfigEntryAuthFailed
import requests

fails = 0
def check(name, cond, info=""):
    global fails
    fails += not cond
    print(("OK  " if cond else "FAIL"), name, info)

TMP = tempfile.mkdtemp()
LEGACY = os.path.join(TMP, "haier_evo")


class FakeStore:
    data = {}
    def __init__(self, key): self.key = key
    async def async_load(self): return FakeStore.data.get(self.key)
    async def async_save(self, d): FakeStore.data[self.key] = d
    async def async_remove(self): FakeStore.data.pop(self.key, None); FakeStore.removed.append(self.key)
FakeStore.removed = []


class FakeHaier:
    behavior = None
    last = None
    def __init__(self, hass, email, password, region, tokens=None, token_saver=None, on_auth_failed=None):
        self.args = dict(email=email, password=password, region=region, tokens=tokens)
        self.devices, self.disconnect_requested, self.stopped = [], False, False
        self.refreshtoken = (tokens or {}).get("refreshtoken")
        self.token_saver, self.on_auth_failed = token_saver, on_auth_failed
        FakeHaier.last = self
        self.pulled = self.connected = False
        self.cached_with = None
    cache_behavior = None
    refresh_results = []
    listed = None
    def pull_data(self):
        self.pulled = True
        FakeHaier.behavior(self)
    def load_cached(self, cached):
        self.cached_with = cached
        if FakeHaier.cache_behavior:
            FakeHaier.cache_behavior(self)
        self.devices = devs(*[c["mac"] for c in cached])
    def connect_in_thread(self): self.connected = True
    def cache_dict(self): return {"version": 1, "devices": [{"mac": d.device_id} for d in self.devices]}
    def refresh_devices(self):
        r = FakeHaier.refresh_results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r
    def list_device_ids(self): return FakeHaier.listed
    def stop(self):
        self.stopped = True
        self.disconnect_requested = True


class FakeEntries:
    def __init__(self, entries): self.entries = entries; self.updates = []
    def async_entries(self, domain): return list(self.entries)
    def async_update_entry(self, entry, **kw):
        self.updates.append(kw)
        if "data" in kw: entry.data = kw["data"]
        if "unique_id" in kw: entry.unique_id = kw["unique_id"]
    async_forward_entry_setups = None
    reloads = None
    def async_schedule_reload(self, entry_id): self.reloads.append(entry_id)


class FakeRegistry:
    def __init__(self, http=False):
        self.removed = []
        self.http = SimpleNamespace(id="http-dev") if http else None
    def async_get_device(self, identifiers): return self.http if ("haier_evo", "haier_evo_http_switch") in identifiers else None
    def async_remove_device(self, dev_id): self.removed.append(dev_id)


def devs(*macs):
    return [SimpleNamespace(device_id=m) for m in macs]


async def run(behavior, known=frozenset(), data=None, unique_id="u", store=None, legacy=None, http=False, others=()):
    FakeHaier.behavior = behavior
    FakeStore.data = dict(store or {})
    if legacy is not None:
        json.dump(legacy, open(LEGACY, "w"))
    elif os.path.exists(LEGACY):
        os.remove(LEGACY)
    tasks, unloads, intervals = [], [], []
    def background(hass, coro, name):
        tasks.append(name); coro.close()
    entry = SimpleNamespace(entry_id="E1", unique_id=unique_id,
                            data=dict(data or {"email": "E@x", "region": "ru"}),
                            async_start_reauth=lambda hass: None,
                            async_create_background_task=background,
                            async_on_unload=unloads.append)
    entries = FakeEntries([entry, *others])
    entries.reloads = []
    entries.async_forward_entry_setups = mock.AsyncMock(return_value=True)
    reg = FakeRegistry(http)

    async def executor(fn, *a): return fn(*a)
    hass = SimpleNamespace(data={}, config_entries=entries, async_add_executor_job=executor,
                           config=SimpleNamespace(path=lambda x: os.path.join(TMP, x)),
                           loop=asyncio.get_running_loop(), loop_thread_id=0)
    with mock.patch.object(integ, "async_get_integration", mock.AsyncMock(return_value=SimpleNamespace(version="t"))), \
         mock.patch.object(integ.api, "Haier", FakeHaier), \
         mock.patch.object(integ, "_token_store", lambda h, eid: FakeStore(f"haier_evo.{eid}")), \
         mock.patch.object(integ, "_devices_store", lambda h, eid: FakeStore(f"haier_evo.{eid}.devices")), \
         mock.patch.object(integ, "async_track_time_interval", lambda h, f, i, **kw: intervals.append(i) or (lambda: None)), \
         mock.patch.object(integ.dr, "async_get", lambda h: reg), \
         mock.patch.object(integ, "_known_device_ids", lambda h, e: set(known)):
        try:
            result = await integ.async_setup_entry(hass, entry)
        except Exception as e:
            result = e
    hass.tasks, hass.intervals = tasks, intervals
    return result, hass, entry, entries, reg


FULL = lambda h: setattr(h, "devices", devs("a", "b"))
TOK = {"token": "T", "refreshtoken": "R"}


async def main():
    def timeout(h): raise requests.exceptions.ConnectTimeout("connect timeout")
    r, hass, *_ = await run(timeout, store={"haier_evo.E1": TOK})
    check("network error -> ConfigEntryNotReady", isinstance(r, ConfigEntryNotReady) and FakeHaier.last.stopped and not hass.data, repr(r))

    def reauth(h): raise api.ReauthRequired("no tokens")
    r, *_ = await run(reauth)
    check("no usable tokens -> ConfigEntryAuthFailed (reauth)", isinstance(r, ConfigEntryAuthFailed), repr(r))

    def bad_pw(h): raise api.AuthUserError("wrong password")
    r, *_ = await run(bad_pw, data={"email": "E@x", "region": "ru", "password": "old"})
    check("rejected password -> ConfigEntryAuthFailed", isinstance(r, ConfigEntryAuthFailed), repr(r))

    r, *_ = await run(lambda h: setattr(h, "devices", []), store={"haier_evo.E1": TOK})
    check("empty device list -> ConfigEntryNotReady", isinstance(r, ConfigEntryNotReady), repr(r))

    known = {"a", "b", "c", "d"}
    res = [(await run(FULL, known, store={"haier_evo.E1": TOK}))[0] for _ in range(4)]
    check("incomplete list -> 3 retries then start",
          all(isinstance(x, ConfigEntryNotReady) for x in res[:3]) and res[3] is True,
          str([type(x).__name__ if not isinstance(x, bool) else x for x in res]))

    # migration from 1.1.x: password in the entry, tokens in /config/haier_evo
    legacy = {"token": "LT", "refreshtoken": "LR", "refreshexpire": "2099-01-01 00:00:00+03:00"}
    r, hass, entry, entries, reg = await run(FULL, data={"email": "E@x", "region": "ru", "password": "secret"},
                                             unique_id=None, legacy=legacy, http=True)
    check("migration: loaded", r is True, repr(r))
    check("migration: legacy tokens moved to the store and passed to the client",
          FakeStore.data.get("haier_evo.E1") == legacy and FakeHaier.last.args["tokens"] == legacy)
    check("migration: password removed from the entry", "password" not in entry.data and entry.data.get("email") == "E@x", str(entry.data))
    check("migration: legacy file deleted", not os.path.exists(LEGACY))
    check("migration: unique_id set", entry.unique_id == "e@x_ru", str(entry.unique_id))
    check("migration: old HTTP debug device removed", reg.removed == ["http-dev"])

    # password stays if the cloud did not give tokens (setup failed)
    r, hass, entry, *_ = await run(timeout, data={"email": "E@x", "region": "ru", "password": "secret"}, legacy=legacy)
    check("failed setup keeps the password and the legacy file", "password" in entry.data and os.path.exists(LEGACY))

    # a second entry exists: the shared legacy file is not used or deleted
    other = SimpleNamespace(entry_id="E2", unique_id="o", data={})
    r, hass, entry, *_ = await run(FULL, data={"email": "E@x", "region": "ru", "password": "secret"},
                                   legacy=legacy, others=[other])
    check("two entries: legacy file neither used nor deleted", FakeHaier.last.args["tokens"] is None and os.path.exists(LEGACY))

    # tokens handed over by the config flow
    r, hass, entry, *_ = await run(FULL, data={"email": "E@x", "region": "ru", "tokens": TOK})
    check("flow tokens -> store, removed from the entry", FakeStore.data.get("haier_evo.E1") == TOK and "tokens" not in entry.data and r is True)

    # remove entry deletes the tokens
    FakeStore.removed.clear()
    entries = FakeEntries([])
    async def executor(fn, *a): return fn(*a)
    hass = SimpleNamespace(config_entries=entries, async_add_executor_job=executor,
                           config=SimpleNamespace(path=lambda x: os.path.join(TMP, x)))
    json.dump(legacy, open(LEGACY, "w"))
    with mock.patch.object(integ, "_token_store", lambda h, eid: FakeStore(f"haier_evo.{eid}")), \
         mock.patch.object(integ, "_devices_store", lambda h, eid: FakeStore(f"haier_evo.{eid}.devices")):
        await integ.async_remove_entry(hass, SimpleNamespace(entry_id="E1"))
    check("remove entry -> tokens, saved devices and legacy file deleted",
          FakeStore.removed == ["haier_evo.E1", "haier_evo.E1.devices"] and not os.path.exists(LEGACY), str(FakeStore.removed))

    # thread-safe saver writes through the event loop and waits
    loop = asyncio.get_running_loop()
    saved = []
    class S:
        async def async_save(self, d): saved.append(d)
    hass = SimpleNamespace(loop=loop, loop_thread_id=-1)
    saver = integ._token_saver(hass, S())
    await loop.run_in_executor(None, saver, {"x": 1})
    check("token saver from a worker thread waits for the write", saved == [{"x": 1}])

    devices = [SimpleNamespace(identifiers={("haier_evo", "haier_evo_http_switch")}),
               SimpleNamespace(identifiers={("haier_evo", "5C:01:3B:00:00:01")})]
    with mock.patch.object(integ.dr, "async_get", lambda h: None), \
         mock.patch.object(integ.dr, "async_entries_for_config_entry", lambda r, e: devices):
        ids = integ._known_device_ids(None, SimpleNamespace(entry_id="E1"))
    check("known devices: only Haier MACs, lower-case", ids == {"5c:01:3b:00:00:01"}, str(ids))

    class NewRegistry:
        calls = []
        def async_get_device_by_identifier(self, identifier, entry_id):
            self.calls.append((identifier, entry_id)); return SimpleNamespace(id="new")
        def async_get_device(self, identifiers): raise AssertionError("deprecated API called")
    reg_new = NewRegistry()
    found = integ._find_device(reg_new, ("haier_evo", "haier_evo_http_switch"), "E1")
    check("device lookup: new registry API with the entry id", found.id == "new" and reg_new.calls == [(("haier_evo", "haier_evo_http_switch"), "E1")])
    check("device lookup: old HA falls back to async_get_device",
          integ._find_device(FakeRegistry(http=True), ("haier_evo", "haier_evo_http_switch"), "E1").id == "http-dev")

    # ---- start from the saved device descriptions (1.4.0)
    CACHE = {"haier_evo.E1.devices": {"version": 1, "devices": [{"mac": "a"}, {"mac": "b"}]}}
    def no_cloud(h): raise AssertionError("the cloud must not be asked")
    r, hass, entry, entries, _ = await run(no_cloud, store={"haier_evo.E1": TOK, **CACHE})
    h = FakeHaier.last
    check("saved descriptions: starts without the cloud", r is True and not h.pulled and h.cached_with == CACHE["haier_evo.E1.devices"]["devices"], repr(r))
    check("saved descriptions: websocket started, platforms set up, background refresh scheduled",
          h.connected and entries.async_forward_entry_setups.await_count == 1 and hass.tasks == ["haier_evo initial refresh"])
    check("periodic refresh every 6 h", [i.total_seconds() for i in hass.intervals] == [6 * 3600])

    def broken(h): raise ValueError("bad cached device")
    FakeHaier.cache_behavior = broken
    FakeStore.removed.clear()
    r, hass, *_ = await run(FULL, store={"haier_evo.E1": TOK, **CACHE})
    FakeHaier.cache_behavior = None
    h = FakeHaier.last
    check("unusable saved descriptions: dropped, cloud start instead",
          r is True and h.pulled and "haier_evo.E1.devices" in FakeStore.removed and hass.tasks == [], repr(r))
    check("cloud start saves the descriptions", FakeStore.data.get("haier_evo.E1.devices") == {"version": 1, "devices": [{"mac": "a"}, {"mac": "b"}]})

    for bad in ({}, {"devices": []}, {"devices": "x"}, {"devices": [1]}):
        check(f"cache {bad!r} ignored", integ._cached_devices(bad) is None)

    # ---- the background refresh
    async def refresh(results, listed=None, until=True, devices=("a", "b"), raise_reauth=False):
        FakeHaier.refresh_results = list(results)
        FakeHaier.listed = listed
        h = FakeHaier(None, "e", None, "ru", tokens=TOK)
        h.devices = devs(*devices)
        store = FakeStore("haier_evo.E1.devices")
        FakeStore.data, FakeStore.removed = {}, []
        entries = FakeEntries([]); entries.reloads = []
        reauth = []
        entry = SimpleNamespace(entry_id="E1", async_start_reauth=lambda hass: reauth.append(1))
        async def executor(fn, *a): return fn(*a)
        hass = SimpleNamespace(config_entries=entries, async_add_executor_job=executor)
        sleeps = []
        async def fake_sleep(s): sleeps.append(s)
        with mock.patch.object(integ.asyncio, "sleep", fake_sleep):
            await integ._refresh(hass, entry, h, store, until_synced=until)
        return SimpleNamespace(saved=FakeStore.data.get("haier_evo.E1.devices"), removed=FakeStore.removed,
                               reloads=entries.reloads, sleeps=sleeps, reauth=reauth, haier=h)

    OK = {"failed": [], "changed": False}
    x = await refresh([OK], listed={"a", "b"})
    check("refresh ok: descriptions saved, no reload", x.saved is not None and x.reloads == [] and x.sleeps == [])
    x = await refresh([{"failed": [], "changed": True}], listed={"a", "b"})
    check("refresh: device described differently -> saved, then reload", x.saved is not None and x.reloads == ["E1"])
    x = await refresh([{"failed": ["b"], "changed": False}, RuntimeError("net"), OK], listed={"a", "b"})
    check("refresh: retried with a growing pause until every device answers", x.sleeps == [5, 10] and x.saved is not None and x.reloads == [], str(x.sleeps))
    x = await refresh([{"failed": ["b"], "changed": False}], listed={"a"})
    check("refresh: a failing device the app no longer lists -> rebuild", x.removed == ["haier_evo.E1.devices"] and x.reloads == ["E1"] and x.saved is None)
    x = await refresh([OK], listed={"a", "b", "c"})
    check("refresh: a new device in the app -> rebuild", x.removed == ["haier_evo.E1.devices"] and x.reloads == ["E1"])
    x = await refresh([OK], listed=None)
    check("refresh: unusable device list (cloud glitch) is ignored", x.saved is not None and x.reloads == [])
    x = await refresh([OK], listed={"a"})
    check("refresh: a device missing from the list but answering stays", x.saved is not None and x.reloads == [] and x.removed == [])
    x = await refresh([{"failed": ["b"], "changed": False}], listed={"a", "b"}, until=False)
    check("periodic refresh: one try, no waiting", x.sleeps == [] and x.saved is not None)
    x = await refresh([api.ReauthRequired("no tokens")])
    check("refresh: rejected sign-in -> stop and ask for the password", x.reauth == [1] and x.haier.stopped and x.saved is None)

asyncio.run(main())
print("FAILS:", fails)
sys.exit(1 if fails else 0)
