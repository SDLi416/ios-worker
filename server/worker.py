"""Single-user iOS build worker. All managed data lives on the pinned external volume."""
from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import plistlib
import queue
import re
import shutil
import signal
import stat
import subprocess
import threading
import time
import uuid
import zipfile
from pathlib import Path, PurePosixPath

VERSION = "2.0.0"
GB = 1_000_000_000
CATEGORIES = {
    "derived_data": "Caches/DerivedData", "source_packages": "Caches/SourcePackages",
    "package_cache": "Caches/PackageCache", "results": "Results", "temporary": "Temporary", "job_logs": "Logs", "session_logs": "Sessions",
}
TERMINAL = {"succeeded", "failed", "cancelled", "interrupted"}


class Problem(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def ident(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", value):
        raise Problem("Identifier must use lowercase letters, digits, hyphens or underscores (1–64 characters)")
    return value


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with open(tmp, "x", encoding="utf-8") as f:
            os.chmod(tmp, 0o600)
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def within(base: Path, value: str | Path, must_exist=True):
    p = Path(value)
    if not p.is_absolute():
        p = base / p
    resolved = p.resolve(strict=must_exist)
    if resolved == base.resolve() or not resolved.is_relative_to(base.resolve()):
        raise Problem("Path escapes the managed directory", 403)
    return resolved


def volume_info(mount):
    try:
        r = subprocess.run(["/usr/sbin/diskutil", "info", "-plist", str(mount)],
                           capture_output=True, check=True, timeout=8)
        return plistlib.loads(r.stdout)
    except Exception as exc:
        raise Problem("External SSD is unavailable; no fallback directory will be created", 503) from exc


def storage_guard(config, reader=volume_info):
    root, mount = Path(config["root"]), Path(config["mount"])
    if root.is_symlink() or not mount.is_mount():
        raise Problem("Expected external SSD is not mounted", 503)
    info = reader(mount)
    if info.get("VolumeUUID") != config["volume_uuid"] or info.get("MountPoint") != str(mount):
        raise Problem("External SSD identity does not match", 503)
    if not root.is_dir() or root.stat().st_dev != mount.stat().st_dev:
        raise Problem("Worker root is not on the expected external SSD", 503)
    try:
        marker = json.loads((root / ".worker-root.json").read_text())
    except (OSError, ValueError) as exc:
        raise Problem("Worker root marker is missing", 503) from exc
    if marker.get("owner") != config["owner"]:
        raise Problem("Worker root marker does not match", 503)
    return info


def acquire_service_lock(config, reader=volume_info):
    """Lock before constructing Worker: recovery must never run in a second server."""
    storage_guard(config, reader)
    root = Path(config["root"])
    parent = within(root, "Service/state")
    if parent.stat().st_dev != root.stat().st_dev:
        raise Problem("Service state is not on the expected external SSD", 503)
    fd = os.open(parent / "server.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise Problem("Service lock is not a regular file", 503)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Problem("iOS Worker is already running; duplicate startup skipped", 409) from exc
        os.ftruncate(fd, 0)
        os.write(fd, (str(os.getpid()) + "\n").encode())
        return fd
    except BaseException:
        os.close(fd)
        raise


def tree_fingerprint(path: Path):
    """Metadata digest for preview/execute consistency; never follows directory symlinks."""
    digest = hashlib.sha256()
    allocated = 0
    newest = path.stat().st_mtime
    protected = path.suffix == ".xcarchive"
    for root, dirs, files in os.walk(path, followlinks=False):
        for name in sorted(dirs + files):
            p = Path(root) / name
            s = p.lstat()
            if p.suffix == ".xcarchive" or name == ".keep":
                protected = True
            rel = p.relative_to(path).as_posix()
            digest.update(json.dumps([rel, s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns,
                                      s.st_mode], separators=(",", ":")).encode())
            if stat.S_ISREG(s.st_mode):
                allocated += s.st_blocks * 512
            newest = max(newest, s.st_mtime)
    s = path.lstat()
    digest.update(f"{s.st_dev}:{s.st_ino}:{s.st_mtime_ns}".encode())
    return {"fingerprint": digest.hexdigest(), "bytes": allocated,
            "last_modified": newest, "protected": protected}


def extract_source(zip_path: Path, target: Path, max_expanded=8 * GB):
    """Reject ZipSlip, links, devices, encrypted archives and APFS name collisions."""
    with zipfile.ZipFile(zip_path) as z:
        items = z.infolist()
        if len(items) > 100_000:
            raise Problem("Too many source files")
        total = 0
        seen = set()
        for item in items:
            name = item.filename
            p = PurePosixPath(name)
            mode = item.external_attr >> 16
            if (not name or not p.parts or "\\" in name or "\x00" in name or p.is_absolute()
                    or ".." in p.parts or ":" in p.parts[0]
                    or any(part.endswith(".xcarchive") for part in p.parts)):
                raise Problem("Unsafe or protected path in source ZIP")
            if item.flag_bits & 1:
                raise Problem("Encrypted ZIP is not supported")
            if stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR):
                raise Problem("Source ZIP cannot contain links or special files; use Git for symlink-based sources")
            key = p.as_posix().rstrip("/").casefold()
            if key in seen:
                raise Problem("Duplicate or case-colliding ZIP entry")
            seen.add(key)
            total += item.file_size
            if total > max_expanded:
                raise Problem("Expanded source exceeds upload limit", 413)
            if item.file_size > 100 * 1024 * 1024 and item.file_size > max(item.compress_size, 1) * 500:
                raise Problem("Suspicious compression ratio")
        target.mkdir(parents=True, exist_ok=False, mode=0o700)
        for item in items:
            dst = within(target, item.filename, must_exist=False)
            if item.is_dir():
                dst.mkdir(parents=True, exist_ok=True, mode=0o700)
                continue
            dst.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with z.open(item) as src, open(dst, "xb") as out:
                shutil.copyfileobj(src, out, 1024 * 1024)
            os.chmod(dst, 0o700 if (item.external_attr >> 16) & 0o111 else 0o600)
    return total


class Worker:
    def __init__(self, config: dict, volume_reader=volume_info):
        self.cfg = config
        self.root = Path(config["root"])
        self.mount = Path(config["mount"])
        self.reader = volume_reader
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.tasks = queue.Queue(maxsize=20)
        self.cancelled = set()
        self.uploads = 0
        self.active = None
        self.thread = None
        self.guard()
        self.state_file = self.root / "Service/state/state.json"
        self.state = json.loads(self.state_file.read_text()) if self.state_file.exists() else {
            "projects": {}, "jobs": {}, "simulators": {}, "plans": {}, "clients": {}}
        for job in self.state["jobs"].values():
            if job["status"] not in TERMINAL:
                pid = job.get("process_id")
                if pid and job.get("process_identity"):
                    current = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "lstart="],
                                             capture_output=True, text=True).stdout.strip()
                    if current == job["process_identity"]:
                        try:
                            os.killpg(pid, signal.SIGTERM)
                        except ProcessLookupError:
                            pass
                job.update(status="interrupted", finished_at=time.time(),
                           error="Worker restarted. Review artifacts before retrying.")
        self.save()

        from development import Development
        from sources import Sources
        self.development = Development(self)
        self.sources = Sources(self)
        self.save()

    def guard(self, admission=False):
        info = storage_guard(self.cfg, self.reader)
        if admission:
            if shutil.disk_usage(self.mount).free < self.cfg["min_external_free_gb"] * GB:
                raise Problem("External SSD free space is below the admission threshold", 507)
            if shutil.disk_usage("/System/Volumes/Data").free < self.cfg["min_internal_free_gb"] * GB:
                raise Problem("Internal disk free space is below the admission threshold", 507)
        return info

    def save(self):
        self.guard()
        atomic_json(self.state_file, self.state)

    def start(self):
        self.thread = threading.Thread(target=self.loop, daemon=True, name="ios-worker-queue")
        self.thread.start()

    def close(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=20)
        self.development.close()

    def managed_dir(self, category, unit):
        self.guard()
        ident(unit)
        parent = self.root / CATEGORIES.get(category, category)
        p = within(self.root, parent / unit, must_exist=False)
        p.mkdir(parents=True, exist_ok=True, mode=0o700)
        marker = p / ".managed.json"
        if marker.exists():
            if json.loads(marker.read_text()).get("owner") != self.cfg["owner"]:
                raise Problem("Unowned managed directory", 409)
            marker.touch()
        else:
            atomic_json(marker, {"owner": self.cfg["owner"], "category": category, "unit": unit})
        return p

    def simulator(self, udid):
        if udid not in self.state["simulators"]:
            raise Problem("Only worker-created simulators may be controlled", 403)
        rec = self.state["simulators"][udid]
        data = Path(rec["reference"])
        expected = self.root / "Simulators" / udid / "data"
        if not data.is_symlink() or data.resolve() != expected.resolve() or not expected.is_dir():
            raise Problem("Worker simulator external data reference changed", 409)
        return rec

    def simulator_list(self):
        self.guard()
        raw = subprocess.run(["/usr/bin/xcrun", "simctl", "list", "devices", "-j"],
                             capture_output=True, text=True, check=True, timeout=30)
        devices = {d["udid"]: d for group in json.loads(raw.stdout)["devices"].values() for d in group}
        return [{**rec, "status": devices.get(udid, {})} for udid, rec in self.snapshot()["simulators"].items()]

    def simulator_inventory(self):
        self.guard()
        def read(args):
            r = subprocess.run(["/usr/bin/xcrun", "simctl", "list", "devices", *args, "-j"],
                               capture_output=True, text=True, check=True, timeout=30)
            return json.loads(r.stdout)["devices"]
        all_devices = read([])
        booted = read(["booted"])
        owned = self.snapshot()["simulators"]
        return {"devices": [{**device, "runtime": runtime, "worker_managed": device["udid"] in owned}
                            for runtime, devices in all_devices.items() for device in devices],
                "booted_ids": [d["udid"] for group in booted.values() for d in group],
                "policy": "Reuse idle existing devices. Unmanaged devices are read-only. No erase/delete."}

    def pack(self, jid, source, dest, kind):
        self.run(jid, ["/usr/bin/ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(source), str(dest)], timeout=600)
        return self.add_artifact(jid, dest, kind)

    def execute(self, jid, req):
        action = req["action"]
        result_dir = self.managed_dir("results", jid)
        if action=='register_device':return self.development.register(jid,req)
        if action in {"session", "ui", "diagnostics", "capture"}:
            return self.development.execute(jid, req)
        if action == "capture_matrix":
            return self.development.matrix(jid, req)
        if action == "attachments":
            return self.export_attachments(jid, req, result_dir)
        if action == "simulator":
            self.development.check_device(req)
            return self.execute_simulator(jid, req, result_dir)
        if action == "git":
            project_id = ident(req["project_id"])
            revision = uuid.uuid4().hex
            source = self.root / "Repos" / project_id / "revisions" / revision
            source.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.run(jid, ["/usr/bin/git", "init", str(source)])
            self.run(jid, ["/usr/bin/git", "-C", str(source), "remote", "add", "origin", req["url"]])
            self.run(jid, ["/usr/bin/git", "-C", str(source), "fetch", "--depth=1", "origin", req["commit"]], timeout=600)
            self.run(jid, ["/usr/bin/git", "-C", str(source), "checkout", "--detach", "FETCH_HEAD"])
            commit, _ = self.run(jid, ["/usr/bin/git", "-C", str(source), "rev-parse", "HEAD"], capture=True)
            if commit.strip().lower() != req["commit"].lower():
                raise Problem("Fetched commit does not match")
            rec = {"id": revision, "source_type": "git", "commit": commit.strip(), "created_at": time.time(),
                   "path": str(source.relative_to(self.root))}
            with self.lock:
                project = self.state["projects"].setdefault(project_id, {"id": project_id, "revisions": {}})
                project["revisions"][revision] = rec
                project["latest_revision"] = revision
                self.save()
            return rec
        if action == "export":
            archive_job = self.get_job(req.get("archive_job_id"))
            archive = archive_job.get("result", {}).get("archive_path")
            if not archive or archive_job["status"] != "succeeded":
                raise Problem("A successful archive job is required")
            archive = within(self.root / "Archives", archive)
            options = req.get("export_options")
            if not isinstance(options, dict) or not options:
                raise Problem("export_options must be an ExportOptions.plist dictionary")
            plist = result_dir / "ExportOptions.plist"
            plist.write_bytes(plistlib.dumps(options))
            out = result_dir / "Export"
            self.run(jid, ["/usr/bin/xcodebuild", "-exportArchive", "-archivePath", str(archive),
                           "-exportPath", str(out), "-exportOptionsPlist", str(plist)], timeout=1800)
            for p in out.iterdir():
                if p.is_file():
                    self.add_artifact(jid, p, "export")
            return {"export_path": str(out)}

        revision = self.revision(req["project_id"], req["revision"])
        source = within(self.root / "Repos", revision["path"] if Path(revision["path"]).is_absolute()
                        else self.root / revision["path"])
        workspace_info = None
        if req.get("stable_workspace", True):
            source, workspace_info = self.sources.workspace(req["project_id"], req["revision"])
        prepfile=self.root/'Service/state/project-preparation'/(req['project_id']+'.json')
        if action=='prepare' and req.get('generate')=='xcodegen':
            atomic_json(prepfile,{'generate':'xcodegen','spec':req.get('spec','project.yml')})
        if prepfile.exists():
            preparation=json.loads(prepfile.read_text());spec=within(source,preparation['spec'])
            self.run(jid,['/opt/homebrew/bin/xcodegen','generate','--spec',str(spec),'--project',str(source),'--use-cache','--cache-path',str(source.parent/'xcodegen-cache')],cwd=source,timeout=120)
        container = req.get("container")
        if container:
            container = within(source, container)
        else:
            workspaces = list(source.glob("*.xcworkspace"))
            candidates = workspaces or list(source.glob("*.xcodeproj"))
            if len(candidates) != 1:
                raise Problem("Specify container relative to the source root (.xcodeproj or .xcworkspace)")
            container = candidates[0]
        if container.suffix not in {".xcodeproj", ".xcworkspace"}:
            raise Problem("Unsupported project container")
        args = ["/usr/bin/xcodebuild", "-workspace" if container.suffix == ".xcworkspace" else "-project", str(container)]
        packages = self.managed_dir("source_packages", req["project_id"])
        package_cache = self.managed_dir("package_cache", req["project_id"])
        args += ["-clonedSourcePackagesDirPath", str(packages), "-packageCachePath", str(package_cache)]
        if action=='prepare':
            if req.get('scheme'):args+=['-scheme',req['scheme']]
            if req.get('locked_packages',False):args+=['-onlyUsePackageVersionsFromResolvedFile']
            output,_=self.run(jid,args+['-resolvePackageDependencies'],cwd=source,timeout=600,capture=True)
            return {'container':str(container.relative_to(source)),'workspace':workspace_info,'output':output[-8000:]}
        if action == "inspect":
            output, _ = self.run(jid, args + ["-list", "-json"], cwd=source, timeout=300, capture=True)
            (result_dir / "schemes.txt").write_text(output)
            self.add_artifact(jid, result_dir / "schemes.txt", "schemes")
            return {"output": output}
        dd = self.managed_dir("derived_data", req["project_id"])
        xcresult = result_dir / "result.xcresult"
        args += ["-scheme", req["scheme"], "-configuration", req.get("configuration", "Debug"),
                 "-derivedDataPath", str(dd), "-resultBundlePath", str(xcresult),
                 "-jobs", str(self.cfg.get("build_jobs", 4))]
        simulator_id = req.get("simulator_id", self.cfg.get("default_simulator"))
        if action == "archive":
            archive = self.managed_dir("Archives", jid) / "App.xcarchive"
            args += ["-destination", "generic/platform=iOS", "-archivePath", str(archive), "archive"]
        elif action == "test":
            simulator_id = self.development.check_device(req)
            self.simulator(simulator_id)
            if req.get("session_id"):
                self.development.stop_controller(self.development.session(req["session_id"]))
                inventory=self.simulator_inventory()
                if simulator_id not in inventory['booted_ids']:
                    raise Problem('Session simulator is no longer booted; close and reopen session',409)
            else:
                self.boot_simulator(jid, simulator_id, require_shutdown=True)
            args += ["-destination", "platform=iOS Simulator,id=" + simulator_id,
                     "-destination-timeout", "120", "-parallel-testing-enabled", "NO",
                     "-maximum-concurrent-test-simulator-destinations", "1", "test"]
            if req.get("test_plan"):
                args += ["-testPlan", req["test_plan"]]
            only = req.get("only_testing", [])
            if not isinstance(only, list) or len(only) > 100:
                raise Problem("only_testing must be a list of test identifiers")
            args += ["-only-testing:" + str(t) for t in only]
        else:
            args += ["-destination", "generic/platform=iOS Simulator", "build"]
        if not req.get("signing", False):
            args += ["CODE_SIGNING_ALLOWED=NO"]
        args += ["COMPILER_INDEX_STORE_ENABLE=NO", "INDEX_ENABLE_DATA_STORE=NO",
                 "CLANG_MODULE_CACHE_PATH=" + str(dd / "ModuleCache.noindex"),
                 "SWIFT_MODULE_CACHE_PATH=" + str(dd / "ModuleCache.noindex")]
        if req.get("locked_packages", False):
            args += ["-onlyUsePackageVersionsFromResolvedFile"]
        failure = None
        try:
            self.run(jid, args, cwd=source, timeout=int(req.get("timeout_seconds", 3600)))
        except Problem as exc:
            failure = exc
        finally:
            if action == "test" and not req.get("session_id") and not req.get("keep_simulator_running", False):
                self.run(jid, ["/usr/bin/xcrun", "simctl", "shutdown", simulator_id], timeout=90, check=False)
        # Preserve diagnostic bundles even for failed tests. Cancellation skips packing.
        summary = None
        if xcresult.exists() and jid not in self.cancelled:
            if action == "test":
                output, code = self.run(jid, ["/usr/bin/xcrun", "xcresulttool", "get", "test-results", "summary",
                                              "--path", str(xcresult), "--compact"], capture=True, check=False)
                if code == 0:
                    try:
                        summary = json.loads(output)
                        atomic_json(result_dir / "test-summary.json", summary)
                        self.add_artifact(jid, result_dir / "test-summary.json", "test_summary")
                    except ValueError:
                        pass
            self.pack(jid, xcresult, result_dir / "result.xcresult.zip", "xcresult")
        if failure:
            raise failure
        result = {"derived_data": str(dd), "xcresult": str(xcresult), "workspace": workspace_info}
        if summary is not None:
            result["test_summary"] = summary
        if action == "archive":
            result["archive_path"] = str(archive)
            self.pack(jid, archive, archive.parent / "App.xcarchive.zip", "archive")
        elif action == "build":
            settings_args = ["/usr/bin/xcodebuild", "-workspace" if container.suffix == ".xcworkspace" else "-project", str(container),
                             "-scheme", req["scheme"], "-configuration", req.get("configuration", "Debug"),
                             "-derivedDataPath", str(dd), "-destination", "generic/platform=iOS Simulator",
                             "-clonedSourcePackagesDirPath", str(packages), "-packageCachePath", str(package_cache),
                             "-showBuildSettings", "-json"]
            output, _ = self.run(jid, settings_args, cwd=source, capture=True, timeout=300)
            settings = None
            for match in re.finditer(r"\[", output):
                try:
                    candidate, _ = json.JSONDecoder().raw_decode(output[match.start():])
                    if isinstance(candidate, list) and candidate and isinstance(candidate[0], dict) and "buildSettings" in candidate[0]:
                        settings = candidate
                        break
                except ValueError:
                    pass
            if settings is None:
                raise Problem("Could not identify application products for this scheme")
            apps = []
            for entry in settings:
                setting = entry["buildSettings"]
                if setting.get("PRODUCT_TYPE") == "com.apple.product-type.application":
                    app = within(dd, Path(setting["TARGET_BUILD_DIR"]) / setting["FULL_PRODUCT_NAME"])
                    if app.is_dir() and app not in apps:
                        apps.append(app)
            result["apps"] = []
            for app in apps:
                # Copy once to Results so later incremental builds cannot change this artifact.
                frozen = result_dir / app.name
                self.run(jid, ["/usr/bin/ditto", str(app), str(frozen)], timeout=300)
                self.pack(jid, frozen, result_dir / (app.name + ".zip"), "simulator_app")
                result["apps"].append(str(frozen))
        return result

    def export_attachments(self,jid,req,result_dir):
        original=self.get_job(req['test_job_id'])
        path=self.root/'Results'/original['id']/'result.xcresult'
        if not path.is_dir():raise Problem('Test result bundle is unavailable',404)
        out=result_dir/'attachments'
        args=['/usr/bin/xcrun','xcresulttool','export','attachments','--path',str(path),'--output-path',str(out)]
        if req.get('only_failures',True):args+=['--only-failures']
        if req.get('test_id'):args+=['--test-id',str(req['test_id'])]
        self.run(jid,args,timeout=120)
        files=[]
        for p in out.rglob('*'):
            if p.is_file():
                files.append({'name':p.name,'bytes':p.stat().st_size})
        self.pack(jid,out,result_dir/'attachments.zip','test_attachments')
        for p in out.glob('*.png'):
            self.add_artifact(jid,p,'test_screenshot')
        return {'files':files[:200],'total':len(files)}

    def boot_simulator(self, jid, udid, require_shutdown=False):
        self.simulator(udid)
        if require_shutdown:
            inventory = self.simulator_inventory()
            device = next((d for d in inventory["devices"] if d["udid"] == udid), None)
            if not device or device.get("state") != "Shutdown" or udid in inventory["booted_ids"]:
                raise Problem("Simulator is already running or unavailable; reuse another idle device or wait", 409)
        # A failed boot in test mode must not claim ownership of an existing running device.
        self.run(jid, ["/usr/bin/xcrun", "simctl", "boot", udid], timeout=120, check=require_shutdown)
        self.run(jid, ["/usr/bin/xcrun", "simctl", "bootstatus", udid, "-b"], timeout=300)

    def execute_simulator(self, jid, req, result_dir):
        op = req.get("operation")
        if op == "create":
            name = req.get("name", "iOS Worker iPhone")
            if not isinstance(name, str) or not 1 <= len(name) <= 80 or name.startswith("-"):
                raise Problem("Invalid simulator name")
            runtime, device = req.get("runtime"), req.get("device_type")
            if not all(isinstance(v, str) and v.startswith("com.apple.CoreSimulator.") for v in [runtime, device]):
                raise Problem("Specify runtime and device_type identifiers")
            if len(self.state["simulators"]) >= 4:
                raise Problem("Worker is limited to four dedicated simulators")
            output, _ = self.run(jid, ["/usr/bin/xcrun", "simctl", "create", name, device, runtime], capture=True)
            udid = str(uuid.UUID(output.strip())).upper()
            ref = Path.home() / "Library/Developer/CoreSimulator/Devices" / udid / "data"
            external = self.root / "Simulators" / udid / "data"
            external.parent.mkdir(mode=0o700)
            # Only this newly created, never-booted simulator is relocated.
            shutil.move(str(ref), str(external))
            ref.symlink_to(external, target_is_directory=True)
            log_ref = Path.home() / "Library/Logs/CoreSimulator" / udid
            log_dest = external.parent / "Logs"
            log_dest.mkdir(mode=0o700)
            if not log_ref.exists() and not log_ref.is_symlink():
                log_ref.parent.mkdir(parents=True, exist_ok=True)
                log_ref.symlink_to(log_dest, target_is_directory=True)
            rec = {"udid": udid, "name": name, "data": str(external), "reference": str(ref)}
            with self.lock:
                self.state["simulators"][udid] = rec
                self.save()
            return rec
        udid = req.get("simulator_id", self.cfg.get("default_simulator"))
        self.simulator(udid)
        base = ["/usr/bin/xcrun", "simctl"]
        if op == "boot":
            self.boot_simulator(jid, udid, require_shutdown=True)
            return {"udid": udid, "state": "Booted"}
        if op == "shutdown":
            args = base + ["shutdown", udid]
        elif op == "install":
            build = self.get_job(req.get("build_job_id"))
            apps = build.get("result", {}).get("apps", [])
            if build["status"] != "succeeded" or not apps:
                raise Problem("install requires a successful simulator build job")
            app_name = req.get("app_name")
            matches = [p for p in apps if not app_name or Path(p).name == app_name]
            if len(matches) != 1:
                raise Problem("Specify app_name from the build result")
            app = within(self.root / "Results", matches[0])
            self.boot_simulator(jid, udid)
            args = base + ["install", udid, str(app)]
        elif op in {"launch", "terminate", "uninstall"}:
            bundle = req.get("bundle_id", "")
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{1,250}", bundle):
                raise Problem("Invalid bundle_id")
            args = base + [op, udid, bundle]
        elif op == "screenshot":
            out = result_dir / "screenshot.png"
            self.run(jid, base + ["io", udid, "screenshot", str(out)], timeout=60)
            return self.add_artifact(jid, out, "screenshot")
        elif op == "video":
            duration = max(1, min(60, int(req.get("duration_seconds", 10))))
            out = result_dir / "screen.mp4"
            args = base + ["io", udid, "recordVideo", "--force", "--codec=h264", str(out)]
            self.run(jid, args, timeout=duration + 30, interrupt_after=duration)
            return self.add_artifact(jid, out, "video")
        elif op == "openurl":
            url = req.get("url", "")
            if not isinstance(url, str) or not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", url) or len(url) > 4000:
                raise Problem("Invalid URL")
            args = base + ["openurl", udid, url]
        elif op == "push":
            payload = req.get("payload")
            bundle = req.get("bundle_id", "")
            if not isinstance(payload, dict) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{1,250}", bundle):
                raise Problem("Specify bundle_id and JSON payload")
            path = result_dir / "push.json"
            atomic_json(path, payload)
            args = base + ["push", udid, bundle, str(path)]
        elif op == "location":
            lat, lon = float(req["latitude"]), float(req["longitude"])
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise Problem("Invalid coordinates")
            args = base + ["location", udid, "set", f"{lat},{lon}"]
        elif op == "privacy":
            mode, service, bundle = req.get("mode"), req.get("service"), req.get("bundle_id", "")
            if mode not in {"grant", "revoke", "reset"} or service not in {
                "all", "calendar", "contacts", "location", "location-always", "photos", "photos-add", "media-library",
                "microphone", "motion", "reminders", "siri", "speech-recognition", "camera"}:
                raise Problem("Unsupported privacy operation")
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{1,250}", bundle):
                raise Problem("Invalid bundle_id")
            args = base + ["privacy", udid, mode, service, bundle]
        else:
            raise Problem("Unsupported simulator operation")
        output, _ = self.run(jid, args, timeout=120, capture=True)
        return {"udid": udid, "operation": op, "output": output}

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.state)

    def client_seen(self, client_id, address, agent):
        if not isinstance(client_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", client_id):
            client_id = "unnamed"
        with self.lock:
            self.state["clients"][client_id] = {"address": address, "user_agent": agent[:200],
                                                "last_seen": time.time()}
            self.save()

    def import_zip(self, project_id, archive):
        ident(project_id)
        with self.lock:
            self.guard(admission=True)
            zip_digest=sha256_file(archive)
            for old in self.state['projects'].get(project_id,{}).get('revisions',{}).values():
                if old.get('sha256')==zip_digest:
                    return dict(copy.deepcopy(old),reused=True)
            revision = uuid.uuid4().hex
            dest = self.root / "Repos" / project_id / "revisions" / revision
            dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                expanded = extract_source(archive, dest)
                record = {"id": revision, "source_type": "zip", "sha256": zip_digest,
                          "created_at": time.time(), "path": str(dest.relative_to(self.root)),
                          "expanded_bytes": expanded}
                project = self.state["projects"].setdefault(project_id, {"id": project_id, "revisions": {}})
                project["revisions"][revision] = record
                project["latest_revision"] = revision
                self.save()
                return copy.deepcopy(record)
            except Exception:
                if dest.exists() and not dest.is_symlink():
                    shutil.rmtree(dest)
                raise

    def revision(self, project_id, revision=None):
        p = self.state["projects"].get(ident(project_id))
        if not p:
            raise Problem("Project not found; upload sources first", 404)
        rev = p["revisions"].get(revision or p["latest_revision"])
        if not rev:
            raise Problem("Revision not found", 404)
        return rev

    def submit(self, request):
        request = copy.deepcopy(request)
        with self.lock:
            self.guard(admission=True)
            if self.tasks.full():
                raise Problem("Queue is full", 429)
            action = request.get("action")
            if action not in {"build", "test", "archive", "inspect", "export", "git", "simulator", "session", "ui", "diagnostics", "capture", "capture_matrix", "attachments", "prepare", "register_device"}:
                raise Problem("Unsupported action")
            if action=='test' or (action=='simulator' and request.get('operation')!='create'):
                self.development.check_device(request)
            if action in {"build", "test", "archive", "inspect", "prepare"}:
                rev = self.revision(request.get("project_id"), request.get("revision"))
                request["revision"] = rev["id"]
                if action not in {"inspect", "prepare"} and not request.get("scheme"):
                    raise Problem("scheme is required")
            if action == "git":
                ident(request.get("project_id"))
                url = request.get("url", "")
                if not (re.fullmatch(r"(?:https|ssh)://[^\s]+", url) or re.fullmatch(r"git@[^\s:]+:[^\s]+", url)):
                    raise Problem("Use an HTTPS or SSH Git URL")
                if not re.fullmatch(r"[0-9a-fA-F]{40}", request.get("commit", "")):
                    raise Problem("Git import requires a full 40-character commit SHA")
            jid = uuid.uuid4().hex
            job = {"id": jid, "status": "queued", "created_at": time.time(),
                   "request": request, "artifacts": []}
            self.state["jobs"][jid] = job
            self.save()
            self.tasks.put_nowait(jid)
            return copy.deepcopy(job)

    def get_job(self, job_id):
        with self.lock:
            job = self.state["jobs"].get(ident(job_id))
            if not job:
                raise Problem("Job not found", 404)
            return copy.deepcopy(job)

    def cancel(self, job_id):
        with self.lock:
            job = self.get_job(job_id)
            if job["status"] not in TERMINAL:
                self.cancelled.add(job_id)
            return {"id": job_id, "cancellation_requested": job["status"] not in TERMINAL}

    def env(self, jid):
        temp = self.managed_dir("temporary", jid)
        env = {k: v for k, v in os.environ.items() if k in {
            "HOME", "USER", "LOGNAME", "SHELL", "LANG", "SSH_AUTH_SOCK", "SECURITYSESSIONID"}}
        env.update(PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
                   DEVELOPER_DIR=self.cfg["developer_dir"], TMPDIR=str(temp) + "/",
                   XDG_CACHE_HOME=str(temp / "xdg-cache"), PYTHONDONTWRITEBYTECODE="1",
                   GIT_TERMINAL_PROMPT="0", GIT_SSH_COMMAND="/usr/bin/ssh -oBatchMode=yes -oStrictHostKeyChecking=yes")
        return env

    def run(self, jid, args, cwd=None, timeout=1800, capture=False, check=True, interrupt_after=None, extra_env=None):
        self.guard()
        logs = self.managed_dir("job_logs", jid)
        logpath = logs / "build.log"
        output = bytearray()
        child_env=self.env(jid)
        child_env.update(extra_env or {})
        process = subprocess.Popen(args, cwd=cwd or self.root, env=child_env,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   start_new_session=True, stdin=subprocess.DEVNULL)
        identity = subprocess.run(["/bin/ps", "-p", str(process.pid), "-o", "lstart="],
                                  capture_output=True, text=True).stdout.strip()
        with self.lock:
            self.state["jobs"][jid].update(process_id=process.pid, process_identity=identity)
            try:
                self.save()
            except Exception:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=10)
                raise

        def drain():
            with open(logpath, "ab", buffering=0) as log:
                log.write(("\n$ " + " ".join(args[:2]) + " [arguments recorded in job request]\n").encode())
                while True:
                    chunk = process.stdout.read(8192)
                    if not chunk:
                        break
                    if log.tell() > 16 * 1024 * 1024:
                        log.seek(0)
                        log.truncate()
                        log.write(b"[Earlier output truncated: 16 MiB log limit]\n")
                    log.write(chunk)
                    if capture:
                        output.extend(chunk)
                        if len(output) > 8 * 1024 * 1024:
                            del output[:len(output) - 8 * 1024 * 1024]
        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        deadline = time.monotonic() + min(timeout, 7200)
        reason = None
        interrupt_at = time.monotonic() + interrupt_after if interrupt_after else None
        try:
            while process.poll() is None:
                if interrupt_at and time.monotonic() >= interrupt_at:
                    try:
                        os.killpg(process.pid, signal.SIGINT)
                    except ProcessLookupError:
                        pass
                    interrupt_at = None
                if jid in self.cancelled or self.stop_event.is_set():
                    reason = "cancelled"
                elif time.monotonic() > deadline:
                    reason = "Command timed out"
                else:
                    try:
                        self.guard()
                        if shutil.disk_usage(self.mount).free < self.cfg["hard_external_free_gb"] * GB:
                            reason = "External SSD reached emergency free-space threshold"
                        if shutil.disk_usage("/System/Volumes/Data").free < self.cfg["hard_internal_free_gb"] * GB:
                            reason = "Internal disk reached emergency free-space threshold"
                    except Problem as exc:
                        reason = str(exc)
                if reason:
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    try:
                        process.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                    break
                time.sleep(1)
        finally:
            process.wait()
            reader.join(timeout=5)
            process.stdout.close()
        if reason:
            raise Problem(reason, 409)
        if check and process.returncode:
            raise Problem(f"Command failed with exit code {process.returncode}; inspect the job log", 422)
        return output.decode(errors="replace"), process.returncode

    def loop(self):
        while not self.stop_event.is_set():
            try:
                jid = self.tasks.get(timeout=1)
            except queue.Empty:
                continue
            with self.lock:
                self.active = jid
                job = self.state["jobs"][jid]
                job.update(status="running", started_at=time.time())
                try:
                    self.save()
                except Problem:
                    job.update(status="failed", error="External SSD unavailable")
            try:
                if jid in self.cancelled:
                    raise Problem("cancelled", 409)
                self.guard(admission=True)
                result = self.execute(jid, job["request"])
                with self.lock:
                    job.update(status="succeeded", result=result)
            except Exception as exc:
                with self.lock:
                    job.update(status="cancelled" if str(exc) == "cancelled" else "failed", error=str(exc))
            finally:
                with self.lock:
                    job.update(finished_at=time.time(), process_id=None, process_identity=None)
                    self.active = None
                    self.cancelled.discard(jid)
                    try:
                        self.save()
                    except Problem:
                        pass
                self.tasks.task_done()

    def add_artifact(self, jid, path, kind):
        path = within(self.root, path)
        if not path.is_file():
            raise Problem("Artifact is not a regular file")
        record = {"name": path.name, "kind": kind, "path": str(path.relative_to(self.root)),
                  "bytes": path.stat().st_size, "sha256": sha256_file(path)}
        with self.lock:
            self.state["jobs"][jid]["artifacts"].append(record)
            self.save()
        return record

    def artifact(self, jid, name):
        job = self.get_job(jid)
        for item in job["artifacts"]:
            if item["name"] == name:
                self.guard()
                try:
                    return within(self.root, item["path"])
                except FileNotFoundError:
                    raise Problem("Artifact was removed by an approved storage plan", 410)
        raise Problem("Artifact not found", 404)

    def storage(self):
        self.guard()
        internal = shutil.disk_usage("/System/Volumes/Data")
        external = shutil.disk_usage(self.mount)
        sizes = {}
        for name in ["Repos", "Caches", "Results", "Archives", "Temporary", "Simulators", "Service", "Logs", "Workspaces", "SourceStore", "Sessions"]:
            p = self.root / name
            if not p.exists():
                sizes[name]={"allocated_bytes":0,"partial":False}
                continue
            r = subprocess.run(["/usr/bin/du", "-skx", str(p)], capture_output=True, text=True, timeout=30)
            sizes[name] = {"allocated_bytes": int(r.stdout.split()[0]) * 1024,
                           "partial": bool(r.stderr)}
        return {"root": str(self.root), "external": dict(zip(["total", "used", "free"], external)),
                "internal": dict(zip(["total", "used", "free"], internal)), "categories": sizes,
                "policy": {k: self.cfg[k] for k in ["min_external_free_gb", "min_internal_free_gb",
                                                     "hard_external_free_gb", "hard_internal_free_gb"]},
                "automatic_deletion": False, "protected": ["Repos", "Archives", "Service", "existing projects and Archives"]}

    def plan_cleanup(self, categories, older_than_days):
        if not categories or any(c not in CATEGORIES for c in categories):
            raise Problem("Only worker-owned cache, temporary and result categories may be cleaned")
        if not isinstance(older_than_days, (int, float)) or not 0 <= older_than_days <= 3650:
            raise Problem("older_than_days must be between 0 and 3650")
        with self.lock:
            self.guard()
            if self.active or not self.tasks.empty() or self.uploads or any(s['status'] in {'active','opening','interrupted'} for s in self.state['sessions'].values()):
                raise Problem("Close development sessions and wait until all jobs and uploads finish", 409)
            entries = []
            for category in dict.fromkeys(categories):
                parent = self.root / CATEGORIES[category]
                if parent.resolve() != parent or not parent.resolve().is_relative_to(self.root.resolve()):
                    raise Problem("Cleanup category must not contain symbolic-link ancestors", 409)
                for p in sorted(parent.iterdir()):
                    if not p.is_dir() or p.is_symlink():
                        continue
                    marker = p / ".managed.json"
                    if not marker.is_file() or marker.is_symlink():
                        continue
                    try:
                        if json.loads(marker.read_text()).get("owner") != self.cfg["owner"]:
                            continue
                        fp = tree_fingerprint(p)
                        if fp["protected"] or fp["last_modified"] > time.time() - older_than_days * 86400:
                            continue
                        entries.append({"category": category, "path": str(p.relative_to(self.root)), **fp})
                    except OSError:
                        continue
            pid = uuid.uuid4().hex
            plan = {"id": pid, "created_at": time.time(), "expires_at": time.time() + 900,
                    "entries": entries, "estimated_bytes": sum(e["bytes"] for e in entries),
                    "executed": False}
            self.state["plans"][pid] = plan
            self.save()
            return copy.deepcopy(plan)

    def execute_cleanup(self, plan_id, confirmation):
        with self.lock:
            self.guard()
            if confirmation != plan_id:
                raise Problem("confirmation must equal the reviewed plan id", 409)
            if self.active or not self.tasks.empty() or self.uploads or any(s['status'] in {'active','opening','interrupted'} for s in self.state['sessions'].values()):
                raise Problem("Storage cleanup is locked while jobs, uploads or development sessions exist", 409)
            plan = self.state["plans"].get(ident(plan_id))
            if not plan or plan["executed"] or time.time() > plan["expires_at"]:
                raise Problem("Plan is missing, expired or already used", 409)
            validated = []
            for entry in plan["entries"]:
                category = entry["category"]
                parent = self.root / CATEGORIES[category]
                if parent.resolve() != parent or not parent.resolve().is_relative_to(self.root.resolve()):
                    raise Problem("Cleanup category path changed", 409)
                raw = self.root / entry["path"]
                if raw.is_symlink() or raw.parent != parent:
                    raise Problem("Cleanup path changed", 409)
                p = within(parent, raw)
                marker = json.loads((p / ".managed.json").read_text())
                fp = tree_fingerprint(p)
                if marker.get("owner") != self.cfg["owner"] or fp["protected"] or fp["fingerprint"] != entry["fingerprint"]:
                    raise Problem("Files changed after preview; create a fresh plan", 409)
                validated.append(p)
            if not shutil.rmtree.avoids_symlink_attacks:
                raise Problem("This Python cannot safely remove directory trees", 503)
            before = shutil.disk_usage(self.mount).free
            deleted = []
            for p in validated:
                self.guard()
                shutil.rmtree(p)
                deleted.append(str(p.relative_to(self.root)))
            plan.update(executed=True, executed_at=time.time(), deleted=deleted,
                        measured_free_change=shutil.disk_usage(self.mount).free - before)
            self.save()
            return copy.deepcopy(plan)
