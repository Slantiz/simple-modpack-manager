"""Offline integration tests for the reconcile → store → materialize pipeline.

No network: a FakeProvider returns versions from an in-memory catalog and a
FakeSession serves jar bytes by URL so the real Store/engine/builder code runs.

Run with:  py -m unittest discover -s tests
"""

from __future__ import annotations

import hashlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from modman import config, engine, history
from modman import lock as lock_io
from modman.model import ResolvedVersion
from modman.providers.base import Provider
from modman.store import Store
from modman.workspace import Workspace


# ── fakes ────────────────────────────────────────────────────────────────────


class FakeResponse:
    def __init__(self, data: bytes):
        self._data = data
        self.headers: dict = {}

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size=65536):
        for i in range(0, len(self._data), chunk_size):
            yield self._data[i : i + chunk_size]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeSession:
    """Serves jar bytes by URL; both provider and store use this."""

    def __init__(self, content: dict[str, bytes]):
        self.content = content
        self.headers: dict = {}

    def get(self, url, headers=None, stream=False, timeout=None):
        return FakeResponse(self.content[url])


class FakeProvider(Provider):
    """Resolves against an in-memory catalog: project_id -> list newest-first.

    Each catalog entry is (version_id, version_number, filename, bytes).
    """

    name = "modrinth"

    def __init__(self, catalog: dict[str, list[tuple]], content: dict[str, bytes]):
        self.catalog = catalog
        self.content = content

    def resolve(self, mod, game_version, loader, session):
        from modman.providers.base import ResolveError

        rank = {"release": 3, "beta": 2, "alpha": 1}
        rtype_of = lambda v: v[4] if len(v) > 4 else "release"  # noqa: E731

        versions = self.catalog.get(mod.project_id)
        if not versions:
            raise ResolveError(f"no such project {mod.project_id}")
        pin = mod.pinned_version()
        if pin is not None:
            chosen = next((v for v in versions if v[1] == pin or v[0] == pin), None)
            if chosen is None:
                raise ResolveError(f"pin {pin} not found for {mod.project_id}")
        else:
            floor = rank.get(mod.channel, 1)
            chosen = next((v for v in versions if rank.get(rtype_of(v), 3) >= floor), None)
            if chosen is None:
                raise ResolveError(f"no {mod.channel}+ build for {mod.project_id}")
        vid, vnum, filename, data = chosen[0], chosen[1], chosen[2], chosen[3]
        url = f"https://fake/{mod.project_id}/{vid}"
        self.content[url] = data
        return ResolvedVersion(
            source="modrinth",
            project_id=mod.project_id,
            version_id=vid,
            version_number=vnum,
            filename=filename,
            download_url=url,
            sha512=hashlib.sha512(data).hexdigest(),
            canonical_id=mod.project_id,
            release_type=rtype_of(chosen),
        )


def make_jar(tag: str) -> bytes:
    return b"JAR:" + tag.encode() + b"\x00" * 32


# ── base fixture ─────────────────────────────────────────────────────────────


class PipelineCase(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.tmp = Path(tempfile.mkdtemp(prefix="modman-test-"))
        self.ws = Workspace(self.tmp)
        self.ws.profiles_dir.mkdir(parents=True)
        self.content: dict[str, bytes] = {}
        self.catalog: dict[str, list[tuple]] = {}
        self.session = FakeSession(self.content)
        self.store = Store(self.ws)

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def registry(self):
        prov = FakeProvider(self.catalog, self.content)
        return {"modrinth": prov, "curseforge": prov, "manual": prov, "url": prov}

    def add_catalog(self, project_id, versions):
        """versions: (version_id, version_number, filename, tag[, release_type]) newest-first."""
        self.catalog[project_id] = [
            (v[0], v[1], v[2], make_jar(v[3]), (v[4] if len(v) > 4 else "release"))
            for v in versions
        ]

    def write_profile(self, pid, body, game="1.21.1", loader="neoforge"):
        text = f'[profile]\nid="{pid}"\ngame_version="{game}"\nloader="{loader}"\n\n' + body
        (self.ws.profiles_dir / f"{pid}.toml").write_text(text, encoding="utf-8")

    def install(self, pid, mode="install", update_keys=None):
        prof = config.load_profile(pid, self.ws)
        lk = lock_io.load(pid, self.ws)
        pl = engine.plan(prof, lk, self.registry(), mode=mode,
                         update_keys=update_keys, session=self.session)
        result = engine.apply(pl, lk, self.registry(), self.store, self.ws,
                              session=self.session)
        return prof, pl, result

    def side_files(self, prof, side):
        d = prof.dir_for_side(side)
        return sorted(p.name for p in d.glob("*.jar")) if d.is_dir() else []

    def store_count(self):
        return len(list(self.store.dir.glob("*.jar")))


# ── tests ────────────────────────────────────────────────────────────────────


class TestInstall(PipelineCase):
    def test_install_materializes_by_side_and_skips_disabled(self):
        self.add_catalog("aaa", [("v1", "1.0", "aaa-1.0.jar", "a1")])
        self.add_catalog("bbb", [("v1", "1.0", "bbb-1.0.jar", "b1")])
        self.add_catalog("ccc", [("v1", "1.0", "ccc-1.0.jar", "c1")])
        self.write_profile(
            "p",
            '[[mods]]\nname="A"\nsource="modrinth"\nid="aaa"\nside="both"\n\n'
            '[[mods]]\nname="B"\nsource="modrinth"\nid="bbb"\nside="server"\n\n'
            '[[mods]]\nname="C"\nsource="modrinth"\nid="ccc"\nside="client"\nenabled=false\n',
        )
        prof, pl, _ = self.install("p")
        self.assertEqual(self.side_files(prof, "client"), ["aaa-1.0.jar"])
        self.assertEqual(self.side_files(prof, "server"),
                         ["aaa-1.0.jar", "bbb-1.0.jar"])
        # disabled C is downloaded/locked but never built
        self.assertEqual(self.store_count(), 3)
        lk = lock_io.load("p", self.ws)
        self.assertFalse(lk.entries["modrinth:ccc"].enabled)

    def test_install_is_idempotent_no_bumps(self):
        self.add_catalog("aaa", [("v2", "2.0", "aaa-2.0.jar", "a2"),
                                 ("v1", "1.0", "aaa-1.0.jar", "a1")])
        self.write_profile("p", '[[mods]]\nname="A"\nsource="modrinth"\nid="aaa"\nside="both"\n')
        self.install("p")
        # A newer v2 exists but install must KEEP v1... wait: first install picks newest v2.
        lk1 = lock_io.load("p", self.ws)
        first = lk1.entries["modrinth:aaa"].version_number
        # Add an even newer version; install must NOT bump.
        self.catalog["aaa"].insert(0, ("v3", "3.0", "aaa-3.0.jar", make_jar("a3")))
        _, pl, _ = self.install("p")
        self.assertTrue(all(i.kind == engine.KEEP for i in pl.items))
        lk2 = lock_io.load("p", self.ws)
        self.assertEqual(lk2.entries["modrinth:aaa"].version_number, first)

    def test_fresh_install_streams_keep_restores(self):
        # After the store is wiped, an install re-fetches the locked (KEEP) jars —
        # and must report each via on_download so the download phase is visible.
        self.add_catalog("aaa", [("v1", "1.0", "aaa-1.0.jar", "a1")])
        self.add_catalog("bbb", [("v1", "1.0", "bbb-1.0.jar", "b1")])
        self.write_profile(
            "p",
            '[[mods]]\nname="A"\nsource="modrinth"\nid="aaa"\nside="both"\n\n'
            '[[mods]]\nname="B"\nsource="modrinth"\nid="bbb"\nside="both"\n',
        )
        self.install("p")
        # Simulate a reset: empty the store; the lock still names both jars.
        for jar in self.store.dir.glob("*.jar"):
            jar.unlink()
        prof = config.load_profile("p", self.ws)
        lk = lock_io.load("p", self.ws)
        pl = engine.plan(prof, lk, self.registry(), mode="install", session=self.session)
        self.assertTrue(all(i.kind == engine.KEEP for i in pl.items))
        streamed = []
        engine.apply(pl, lk, self.registry(), self.store, self.ws,
                     session=self.session, on_download=lambda i: streamed.append(i.key))
        self.assertEqual(sorted(streamed), ["modrinth:aaa", "modrinth:bbb"])
        self.assertEqual(self.store_count(), 2)


class TestDriftAndRemoval(PipelineCase):
    def test_drift_is_healed_on_install(self):
        self.add_catalog("aaa", [("v1", "1.0", "aaa-1.0.jar", "a1")])
        self.write_profile("p", '[[mods]]\nname="A"\nsource="modrinth"\nid="aaa"\nside="client"\n')
        prof, _, _ = self.install("p")
        victim = prof.dir_for_side("client") / "aaa-1.0.jar"
        victim.unlink()
        self.assertFalse(victim.exists())
        self.install("p")  # heal
        self.assertTrue(victim.exists())

    def test_remove_from_toml_prunes_and_sweeps(self):
        self.add_catalog("aaa", [("v1", "1.0", "aaa-1.0.jar", "a1")])
        self.add_catalog("bbb", [("v1", "1.0", "bbb-1.0.jar", "b1")])
        self.write_profile(
            "p",
            '[[mods]]\nname="A"\nsource="modrinth"\nid="aaa"\nside="both"\n\n'
            '[[mods]]\nname="B"\nsource="modrinth"\nid="bbb"\nside="both"\n',
        )
        self.install("p")
        self.assertEqual(self.store_count(), 2)
        self.write_profile("p", '[[mods]]\nname="A"\nsource="modrinth"\nid="aaa"\nside="both"\n')
        prof, pl, res = self.install("p")
        self.assertEqual(self.store_count(), 1)  # bbb swept
        self.assertNotIn("bbb-1.0.jar", self.side_files(prof, "client"))

    def test_builder_never_deletes_unmanaged_jars(self):
        self.add_catalog("aaa", [("v1", "1.0", "aaa-1.0.jar", "a1")])
        self.write_profile("p", '[[mods]]\nname="A"\nsource="modrinth"\nid="aaa"\nside="client"\n')
        prof, _, _ = self.install("p")
        stray = prof.dir_for_side("client") / "some-hand-placed-mod.jar"
        stray.write_bytes(b"user data")
        self.install("p")
        self.assertTrue(stray.exists(), "unmanaged jar must be left alone")


class TestUpdatePin(PipelineCase):
    def test_update_bumps_unpinned_but_holds_pinned(self):
        self.add_catalog("aaa", [("v1", "1.0", "aaa-1.0.jar", "a1")])
        self.add_catalog("bbb", [("v1", "1.0", "bbb-1.0.jar", "b1")])
        self.write_profile(
            "p",
            '[[mods]]\nname="A"\nsource="modrinth"\nid="aaa"\nside="both"\n\n'
            '[[mods]]\nname="B"\nsource="modrinth"\nid="bbb"\nside="both"\npin=true\n',
        )
        self.install("p")
        # newer versions appear
        self.catalog["aaa"].insert(0, ("v2", "2.0", "aaa-2.0.jar", make_jar("a2")))
        self.catalog["bbb"].insert(0, ("v2", "2.0", "bbb-2.0.jar", make_jar("b2")))
        prof, pl, _ = self.install("p", mode="update")
        by_name = {i.name: i for i in pl.items}
        self.assertEqual(by_name["A"].kind, engine.UPDATE)
        self.assertEqual(by_name["B"].kind, engine.HELD)  # pinned, newer reported
        self.assertEqual(by_name["B"].available, "2.0")
        lk = lock_io.load("p", self.ws)
        self.assertEqual(lk.entries["modrinth:aaa"].version_number, "2.0")
        self.assertEqual(lk.entries["modrinth:bbb"].version_number, "1.0")

    def test_explicit_pin_applied_on_install(self):
        self.add_catalog("aaa", [("v2", "2.0", "aaa-2.0.jar", "a2"),
                                 ("v1", "1.0", "aaa-1.0.jar", "a1")])
        self.write_profile("p", '[[mods]]\nname="A"\nsource="modrinth"\nid="aaa"\nside="both"\n')
        self.install("p")  # picks 2.0
        self.assertEqual(lock_io.load("p", self.ws).entries["modrinth:aaa"].version_number, "2.0")
        # Pin back to 1.0 in TOML; install must honor it (deterministic, user-directed).
        self.write_profile("p", '[[mods]]\nname="A"\nsource="modrinth"\nid="aaa"\nside="both"\npin="1.0"\n')
        _, pl, _ = self.install("p")
        self.assertEqual(lock_io.load("p", self.ws).entries["modrinth:aaa"].version_number, "1.0")


class TestSweepAcrossProfiles(PipelineCase):
    def test_shared_jar_survives_when_one_profile_drops_it(self):
        # Two profiles share mod 'aaa' (identical bytes -> same hash).
        self.add_catalog("aaa", [("v1", "1.0", "aaa-1.0.jar", "a1")])
        self.add_catalog("bbb", [("v1", "1.0", "bbb-1.0.jar", "b1")])
        self.write_profile("p1", '[[mods]]\nname="A"\nsource="modrinth"\nid="aaa"\nside="both"\n')
        self.write_profile(
            "p2",
            '[[mods]]\nname="A"\nsource="modrinth"\nid="aaa"\nside="both"\n\n'
            '[[mods]]\nname="B"\nsource="modrinth"\nid="bbb"\nside="both"\n',
        )
        self.install("p1")
        self.install("p2")
        self.assertEqual(self.store_count(), 2)  # aaa (shared) + bbb
        # p2 drops A; aaa must survive because p1 still references it.
        self.write_profile("p2", '[[mods]]\nname="B"\nsource="modrinth"\nid="bbb"\nside="both"\n')
        self.install("p2")
        hashes = {p.stem for p in self.store.dir.glob("*.jar")}
        aaa_hash = hashlib.sha512(make_jar("a1")).hexdigest()
        self.assertIn(aaa_hash, hashes, "shared jar must not be swept")


class TestAtomicFailure(PipelineCase):
    def test_hard_failure_commits_nothing(self):
        self.add_catalog("aaa", [("v1", "1.0", "aaa-1.0.jar", "a1")])
        self.write_profile("p", '[[mods]]\nname="A"\nsource="modrinth"\nid="aaa"\nside="both"\n')
        self.install("p")
        before = lock_io.load("p", self.ws).entries.copy()
        # add a mod that can't resolve
        self.write_profile(
            "p",
            '[[mods]]\nname="A"\nsource="modrinth"\nid="aaa"\nside="both"\n\n'
            '[[mods]]\nname="X"\nsource="modrinth"\nid="missing"\nside="both"\n',
        )
        prof = config.load_profile("p", self.ws)
        lk = lock_io.load("p", self.ws)
        pl = engine.plan(prof, lk, self.registry(), mode="install", session=self.session)
        self.assertTrue(pl.has_hard_failures)
        with self.assertRaises(engine.EngineError):
            engine.apply(pl, lk, self.registry(), self.store, self.ws, session=self.session)
        after = lock_io.load("p", self.ws).entries
        self.assertEqual(set(before), set(after), "lock must be unchanged after abort")


class TestManualCapture(PipelineCase):
    def test_manual_mod_with_file_is_captured_from_side_folder(self):
        # exercises the engine CAPTURED path -> Store.add_file
        self.write_profile(
            "p",
            '[[mods]]\nname="Hand"\nsource="manual"\nid="hand-mod"\nside="client"\n'
            'file="hand-mod-1.0.jar"\n',
        )
        prof = config.load_profile("p", self.ws)
        placed = prof.dir_for_side("client") / "hand-mod-1.0.jar"
        placed.parent.mkdir(parents=True, exist_ok=True)
        placed.write_bytes(make_jar("hand"))
        _, pl, _ = self.install("p")
        kinds = {i.name: i.kind for i in pl.items}
        self.assertEqual(kinds["Hand"], engine.MANUAL)  # manual is always reported
        lk = lock_io.load("p", self.ws)
        self.assertIn("manual:hand-mod", lk.entries)  # ...but still captured + tracked
        self.assertEqual(self.store_count(), 1)  # jar was ingested into the store

    def test_manual_mod_missing_file_is_flagged_not_present(self):
        self.write_profile(
            "p",
            '[[mods]]\nname="Hand"\nsource="manual"\nid="hand-mod"\nside="client"\n'
            'file="hand-mod-1.0.jar"\n',
        )
        prof = config.load_profile("p", self.ws)
        pl = engine.plan(prof, lock_io.load("p", self.ws), self.registry(),
                         mode="install", session=self.session)
        item = next(i for i in pl.items if i.name == "Hand")
        self.assertEqual(item.kind, engine.MANUAL)
        self.assertEqual(item.note, "not present")

    def test_manual_jar_is_retained_after_removal(self):
        # A manual jar can't be re-downloaded, so it must survive the sweep even once
        # nothing in the lock references it — that keeps rollback working.
        self.write_profile(
            "p", '[[mods]]\nname="Hand"\nsource="manual"\nid="hand-mod"\nside="client"\n')
        prof = config.load_profile("p", self.ws)
        jar = self.tmp / "hand-1.0.jar"
        jar.write_bytes(make_jar("hand"))
        mod = next(m for m in prof.mods if m.name == "Hand")
        saved, _, _ = engine.capture_manual(prof, mod, jar, self.store, self.ws)
        sha = saved.entries["manual:hand-mod"].sha512
        self.assertTrue(self.store.has(sha))
        self.assertIn(sha, self.store.manual_hashes())

        # remove the mod from the TOML and install: lock no longer references the jar
        self.write_profile("p", "")
        self.install("p")
        self.assertNotIn("manual:hand-mod", lock_io.load("p", self.ws).entries)
        # ...but the jar is still in the store (retained), unlike a normal mod
        self.assertTrue(self.store.has(sha))

    def test_manual_mod_without_file_is_flagged_then_added(self):
        # No `file` field: nothing to hardcode. It's "not present" until `add`,
        # then capture_manual ingests + builds it and it stays tracked.
        self.write_profile(
            "p",
            '[[mods]]\nname="Hand"\nsource="manual"\nid="hand-mod"\nside="client"\n',
        )
        prof = config.load_profile("p", self.ws)
        pl = engine.plan(prof, lock_io.load("p", self.ws), self.registry(),
                         mode="install", session=self.session)
        item = next(i for i in pl.items if i.name == "Hand")
        self.assertEqual(item.note, "not present")

        jar = self.tmp / "hand-2.0.jar"
        jar.write_bytes(make_jar("hand2"))
        mod = next(m for m in prof.mods if m.name == "Hand")
        saved, sides, _ = engine.capture_manual(prof, mod, jar, self.store, self.ws)

        self.assertIn("manual:hand-mod", saved.entries)
        self.assertEqual(saved.entries["manual:hand-mod"].filename, "hand-2.0.jar")
        self.assertEqual(self.side_files(prof, "client"), ["hand-2.0.jar"])
        self.assertEqual(self.store_count(), 1)
        # a follow-up install must keep it (store-backed), not re-flag it
        pl2 = engine.plan(prof, lock_io.load("p", self.ws), self.registry(),
                          mode="install", session=self.session)
        item2 = next(i for i in pl2.items if i.name == "Hand")
        self.assertNotEqual(item2.note, "not present")


class TestDependencyChecks(PipelineCase):
    def _lock(self, *entries):
        from modman.model import Lock
        return Lock(profile_id="p", game_version="1.21.1", loader="neoforge",
                    entries={e.key: e for e in entries})

    def _entry(self, key, name, *, cid=None, deps=(), side="both", enabled=True):
        from modman.model import LockEntry
        return LockEntry(key=key, name=name, source="modrinth", project_id=key,
                         version_id="v", version_number="1", filename=key + ".jar",
                         sha512="x" * 8, side=side, enabled=enabled,
                         canonical_id=cid, dependencies=tuple(deps))

    def test_absent_and_cross_source_deps_are_not_warned(self):
        from modman import checks
        # dep ids not present anywhere in the lock (e.g. a CurseForge numeric id, or a
        # library installed under a different source) must NOT produce warnings.
        lk = self._lock(self._entry("mc", "More Culling", cid="mc", deps=["238222", "sbpqhzIG"]))
        self.assertEqual(checks.missing_dependencies(lk), [])

    def test_dependency_present_on_wrong_side_is_warned(self):
        from modman import checks
        lk = self._lock(
            self._entry("voxy", "Voxy WorldGen", cid="voxy", deps=["cloth"], side="both"),
            self._entry("cloth", "Cloth Config API", cid="cloth", side="client"),
        )
        issues = checks.missing_dependencies(lk)
        self.assertEqual(len(issues), 1)
        self.assertIn("Cloth Config API", issues[0].message)
        self.assertIn("not on server", issues[0].message)


class TestDatapack(PipelineCase):
    def test_datapack_routes_to_datapacks_folder_not_mods(self):
        self.add_catalog("dp", [("v1", "1.0", "pack-1.0.zip", "d1")])
        self.add_catalog("aaa", [("v1", "1.0", "aaa-1.0.jar", "a1")])
        self.write_profile(
            "p",
            '[[mods]]\nname="DP"\nsource="modrinth"\nid="dp"\nside="both"\ntype="datapack"\n\n'
            '[[mods]]\nname="A"\nsource="modrinth"\nid="aaa"\nside="both"\n',
        )
        prof, _, _ = self.install("p")
        # the mod goes to client/server; the datapack does NOT
        self.assertEqual(self.side_files(prof, "client"), ["aaa-1.0.jar"])
        self.assertEqual(self.side_files(prof, "server"), ["aaa-1.0.jar"])
        dp = (
            sorted(p.name for p in prof.datapacks_dir.iterdir() if p.is_file())
            if prof.datapacks_dir.is_dir()
            else []
        )
        self.assertEqual(dp, ["pack-1.0.zip"])
        self.assertTrue(lock_io.load("p", self.ws).entries["modrinth:dp"].is_datapack)


class TestSingleplayer(PipelineCase):
    def _sp_files(self, prof):
        d = prof.singleplayer_dir
        return sorted(p.name for p in d.glob("*.jar")) if d.is_dir() else []

    def test_singleplayer_folder_is_client_union_server(self):
        self.add_catalog("aaa", [("v1", "1.0", "aaa-1.0.jar", "a1")])
        self.add_catalog("bbb", [("v1", "1.0", "bbb-1.0.jar", "b1")])
        self.add_catalog("ccc", [("v1", "1.0", "ccc-1.0.jar", "c1")])
        self.write_profile(
            "p",
            "singleplayer = true\n\n"
            '[[mods]]\nname="A"\nsource="modrinth"\nid="aaa"\nside="both"\n\n'
            '[[mods]]\nname="B"\nsource="modrinth"\nid="bbb"\nside="server"\n\n'
            '[[mods]]\nname="C"\nsource="modrinth"\nid="ccc"\nside="client"\n',
        )
        prof, _, _ = self.install("p")
        self.assertEqual(self.side_files(prof, "client"), ["aaa-1.0.jar", "ccc-1.0.jar"])
        self.assertEqual(self.side_files(prof, "server"), ["aaa-1.0.jar", "bbb-1.0.jar"])
        # singleplayer gets everything client-runnable: the union of client + server
        self.assertEqual(self._sp_files(prof),
                         ["aaa-1.0.jar", "bbb-1.0.jar", "ccc-1.0.jar"])

    def test_no_singleplayer_folder_when_not_opted_in(self):
        self.add_catalog("aaa", [("v1", "1.0", "aaa-1.0.jar", "a1")])
        self.write_profile("p", '[[mods]]\nname="A"\nsource="modrinth"\nid="aaa"\nside="server"\n')
        prof, _, _ = self.install("p")
        self.assertFalse(prof.singleplayer_dir.exists())

    def _entry_targets(self, **kw):
        from modman.builder import entry_targets
        from modman.model import LockEntry
        self.write_profile(
            "p", "singleplayer = true\n\n"
            '[[mods]]\nname="A"\nsource="modrinth"\nid="aaa"\nside="server"\n')
        prof = config.load_profile("p", self.ws)
        e = LockEntry(key="modrinth:aaa", name="A", source="modrinth", project_id="aaa",
                      version_id="v1", version_number="1.0", filename="aaa-1.0.jar",
                      sha512="x", side="server", **kw)
        return prof, entry_targets(e, prof)

    def test_server_side_mod_is_included_in_singleplayer(self):
        # client_side unsupported but server_side required (e.g. Noisium): a
        # singleplayer world runs the integrated server, so it belongs in singleplayer/.
        prof, targets = self._entry_targets(client_side="unsupported", server_side="required")
        self.assertIn(prof.server_dir, targets)
        self.assertIn(prof.singleplayer_dir, targets)

    def test_mod_unsupported_on_both_sides_excluded_from_singleplayer(self):
        prof, targets = self._entry_targets(client_side="unsupported", server_side="unsupported")
        self.assertNotIn(prof.singleplayer_dir, targets)


class TestInstallConstraints(PipelineCase):
    """install must leave the lock satisfying the TOML — honoring channel and a
    changed game_version/loader — without chasing 'newest' for valid mods."""

    def test_install_reresolves_when_channel_tightened(self):
        self.add_catalog("aaa", [
            ("v3", "3.0", "aaa-3.0.jar", "a3", "alpha"),
            ("v2", "2.0", "aaa-2.0.jar", "a2", "release"),
            ("v1", "1.0", "aaa-1.0.jar", "a1", "release"),
        ])
        self.write_profile("p", '[[mods]]\nname="A"\nsource="modrinth"\nid="aaa"\nside="both"\n')
        self.install("p")  # default channel = alpha -> newest = 3.0 (alpha)
        e = lock_io.load("p", self.ws).entries["modrinth:aaa"]
        self.assertEqual((e.version_number, e.release_type), ("3.0", "alpha"))

        # tighten to release; install (not update) must move it to newest release
        self.write_profile(
            "p", '[[mods]]\nname="A"\nsource="modrinth"\nid="aaa"\nside="both"\nchannel="release"\n')
        _, pl, _ = self.install("p")
        e2 = lock_io.load("p", self.ws).entries["modrinth:aaa"]
        self.assertEqual((e2.version_number, e2.release_type), ("2.0", "release"))
        self.assertEqual([i.kind for i in pl.items if i.name == "A"], [engine.REPIN])

    def test_install_keeps_when_channel_already_satisfied(self):
        self.add_catalog("aaa", [("v1", "1.0", "aaa-1.0.jar", "a1", "release")])
        self.write_profile(
            "p", '[[mods]]\nname="A"\nsource="modrinth"\nid="aaa"\nside="both"\nchannel="release"\n')
        self.install("p")
        _, pl, _ = self.install("p")
        self.assertTrue(all(i.kind == engine.KEEP for i in pl.items))

    def test_install_reresolves_on_game_version_change(self):
        self.add_catalog("aaa", [("v1", "1.0", "aaa-1.0.jar", "a1", "release")])
        self.write_profile("p", '[[mods]]\nname="A"\nsource="modrinth"\nid="aaa"\nside="both"\n')
        self.install("p")
        # a newer build appears AND the game version changes -> install re-resolves
        # (whereas a plain game-version-unchanged install would KEEP v1)
        self.catalog["aaa"].insert(0, ("v2", "2.0", "aaa-2.0.jar", make_jar("a2"), "release"))
        self.write_profile(
            "p", '[[mods]]\nname="A"\nsource="modrinth"\nid="aaa"\nside="both"\n', game="1.21.4")
        _, pl, _ = self.install("p")
        self.assertEqual(lock_io.load("p", self.ws).entries["modrinth:aaa"].version_number, "2.0")
        self.assertEqual([i.kind for i in pl.items if i.name == "A"], [engine.REPIN])

    def test_unknown_release_type_is_not_rechurned(self):
        # a lock written before release_type was tracked must not be force-changed.
        from modman.model import Lock, LockEntry
        self.add_catalog("aaa", [("v2", "2.0", "aaa-2.0.jar", "a2", "release"),
                                 ("v1", "1.0", "aaa-1.0.jar", "a1", "alpha")])
        self.write_profile(
            "p", '[[mods]]\nname="A"\nsource="modrinth"\nid="aaa"\nside="both"\nchannel="release"\n')
        prof = config.load_profile("p", self.ws)
        entry = LockEntry(key="modrinth:aaa", name="A", source="modrinth", project_id="aaa",
                          version_id="v1", version_number="1.0", filename="aaa-1.0.jar",
                          sha512="z" * 8, side="both", release_type="")
        lk = Lock(profile_id="p", game_version="1.21.1", loader="neoforge",
                  entries={"modrinth:aaa": entry})
        pl = engine.plan(prof, lk, self.registry(), mode="install", session=self.session)
        self.assertEqual(next(i for i in pl.items if i.name == "A").kind, engine.KEEP)


class TestRollback(PipelineCase):
    def test_rollback_restores_snapshot(self):
        self.add_catalog("aaa", [("v1", "1.0", "aaa-1.0.jar", "a1")])
        self.write_profile("p", '[[mods]]\nname="A"\nsource="modrinth"\nid="aaa"\nside="both"\n')
        self.install("p")
        lk = lock_io.load("p", self.ws)
        snap = history.save("p", lk, "good", self.ws)
        # bump A to 2.0
        self.catalog["aaa"].insert(0, ("v2", "2.0", "aaa-2.0.jar", make_jar("a2")))
        prof, _, _ = self.install("p", mode="update")
        self.assertEqual(lock_io.load("p", self.ws).entries["modrinth:aaa"].version_number, "2.0")
        # rollback
        cur = lock_io.load("p", self.ws)
        history.rollback(prof, cur, snap, self.registry(), self.store, self.ws,
                         session=self.session)
        back = lock_io.load("p", self.ws)
        self.assertEqual(back.entries["modrinth:aaa"].version_number, "1.0")
        self.assertEqual(self.side_files(prof, "client"), ["aaa-1.0.jar"])


class TestReset(PipelineCase):
    def test_reset_wipes_store_and_mods(self):
        import argparse
        import contextlib
        import io

        from modman import cli
        self.add_catalog("aaa", [("v1", "1.0", "aaa-1.0.jar", "a1")])
        self.write_profile("p", '[[mods]]\nname="A"\nsource="modrinth"\nid="aaa"\nside="both"\n')
        prof, _, _ = self.install("p")
        self.assertTrue(self.store_count() > 0)
        self.assertTrue(prof.dir_for_side("client").is_dir())

        with contextlib.redirect_stdout(io.StringIO()):
            rc = cli.cmd_reset(argparse.Namespace(yes=True), self.ws)
        self.assertEqual(rc, 0)
        self.assertFalse(self.ws.store_dir.exists())
        self.assertFalse(self.ws.mods_dir.exists())
        # lockfile (state) is kept, so install can rebuild
        self.assertTrue(self.ws.lock_path("p").exists())


class TestCheckOutput(unittest.TestCase):
    """The check-style report rendering (icons, sections, summary)."""

    def _lock(self, key, name, source, filename):
        from modman.model import LockEntry
        return LockEntry(key=key, name=name, source=source, project_id=key.split(":")[1],
                         version_id="1", version_number="1.0", filename=filename,
                         sha512="x", side="both")

    def _resolved(self, source, filename):
        from modman.model import ResolvedVersion
        return ResolvedVersion(source=source, project_id="b", version_id="2",
                               version_number="2.0", filename=filename, download_url="u")

    def test_check_line_icons_and_status(self):
        from modman import report
        keep = engine.PlanItem(key="modrinth:a", name="A", kind=engine.KEEP,
                               existing=self._lock("modrinth:a", "A", "modrinth", "a-1.0.jar"))
        upd = engine.PlanItem(key="modrinth:b", name="B", kind=engine.UPDATE,
                              resolved=self._resolved("curseforge", "b-2.0.jar"))
        man = engine.PlanItem(key="manual:c", name="C", kind=engine.MANUAL,
                              url="http://x/dl",
                              existing=self._lock("manual:c", "C", "manual", "c.jar"))
        fail = engine.PlanItem(key="modrinth:d", name="D", kind=engine.FAIL,
                               error="not found")
        self.assertIn("[=]", report.check_line(keep, 10))
        self.assertIn("up to date", report.check_line(keep, 10))
        self.assertIn("[Modrinth]", report.check_line(keep, 10))
        self.assertIn("[↑]", report.check_line(upd, 10))
        self.assertIn("update available", report.check_line(upd, 10))
        self.assertIn("[CurseForge]", report.check_line(upd, 10))
        self.assertIn("[M]", report.check_line(man, 10))
        self.assertIn("[!]", report.check_line(fail, 10))
        self.assertIn("not found", report.check_line(fail, 10))

    def test_removed_and_summary_sections(self):
        from modman import report
        items = [
            engine.PlanItem(key="modrinth:a", name="A", kind=engine.KEEP,
                            existing=self._lock("modrinth:a", "A", "modrinth", "a.jar")),
            engine.PlanItem(key="modrinth:b", name="B", kind=engine.UPDATE,
                            resolved=self._resolved("modrinth", "b-2.0.jar")),
            engine.PlanItem(key="manual:c", name="C", kind=engine.MANUAL, url="http://x"),
            engine.PlanItem(key="modrinth:d", name="D", kind=engine.FAIL, error="err"),
            engine.PlanItem(key="modrinth:e", name="E", kind=engine.REMOVE,
                            existing=self._lock("modrinth:e", "E", "modrinth", "e-1.jar")),
        ]
        pl = engine.Plan(profile=None, items=items)
        removed = report.check_removed(pl)
        self.assertIn("[-]", removed)
        self.assertIn("e-1.jar", removed)
        summary = report.check_summary(pl)
        self.assertIn("Up to date : 1", summary)
        self.assertIn("To update  : 1", summary)
        self.assertIn("Manual     : 1", summary)
        self.assertIn("Not found  : 1", summary)


class TestOrderedStream(unittest.TestCase):
    def test_flushes_in_fixed_order_regardless_of_completion(self):
        import contextlib
        import io

        from modman import report
        s = report.OrderedStream(["a", "b", "c", "d"], label="x")
        s.footer = False  # deterministic capture
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            s.submit("c", "C")  # held (waiting on a, b)
            s.submit("a", "A")  # flush A
            self.assertEqual(buf.getvalue().split(), ["A"])  # C still held
            s.submit("b", "B")  # flush B, then C
            s.submit("d", "D")  # flush D
            s.finish()
        self.assertEqual(buf.getvalue().split(), ["A", "B", "C", "D"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
