"""Deployment configuration must match the application it deploys.

Docker is not available in every environment that runs these tests, so instead of
starting the stack we statically verify the parts that silently fail at runtime:
an env var the app does not read, a build context that points at a missing file,
or a proxy upstream that names a service that does not exist.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import yaml
from app.core.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[3]


def _compose() -> dict:
    return yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())


def _vercel() -> dict:
    return json.loads((REPO_ROOT / "vercel.json").read_text())


def test_vercel_services_route_public_traffic_and_bind_the_internal_processor():
    config = _vercel()
    services = config["services"]
    assert set(services) == {"api", "processor", "web"}
    assert services["api"]["root"] == "apps/api"
    assert services["api"]["entrypoint"] == "app.main:app"
    assert services["processor"]["runtime"] == "container"
    assert services["processor"]["entrypoint"] == "Dockerfile"
    # Container services use the image's CMD/EXPOSE settings. `command` is not
    # a supported service-level Vercel setting and causes deployment validation
    # to fail, so keep the processor startup contract in its Dockerfile.
    processor_dockerfile = (REPO_ROOT / "apps/processor/rust/Dockerfile").read_text()
    assert 'EXPOSE 8100' in processor_dockerfile
    assert 'CMD ["clipforge-processor", "serve", "0.0.0.0:8100"]' in processor_dockerfile

    assert services["api"]["bindings"] == [
        {"type": "service", "service": "processor", "format": "url", "env": "PROCESSOR_URL"}
    ]
    # The processor has no top-level rewrite, so it remains internal. API and
    # WebSocket rules must precede the web catch-all.
    rewrites = config["rewrites"]
    targets = [(rule["source"], rule["destination"]["service"]) for rule in rewrites]
    assert targets[0] == ("/api/(.*)", "api")
    assert ("/ws/(.*)", "api") in targets
    assert targets[-1] == ("/(.*)", "web")
    assert all(service != "processor" for _, service in targets)


def test_api_runtime_requirements_exclude_worker_only_media_packages():
    api_requirements = (REPO_ROOT / "apps/api/requirements.txt").read_text().lower()
    worker_requirements = (REPO_ROOT / "apps/api/requirements-worker.txt").read_text().lower()
    dev_requirements = (REPO_ROOT / "apps/api/requirements-dev.txt").read_text().lower()

    # These dependencies are imported only by processing handlers or their
    # optional media fallbacks; Vercel's API requirements must not pull them in.
    worker_only = (
        "yt-dlp==",
        "av==",
        "numpy==",
        "pillow==",
        "opencv-python-headless==",
        "imageio-ffmpeg==",
        "espeakng-loader==",
        "espeakng==",
    )
    for package in worker_only:
        assert package not in api_requirements, f"{package} leaked into the Vercel API bundle"
        assert package in worker_requirements, f"{package} is missing from the worker image"

    assert "-r requirements.txt" in worker_requirements
    assert "-r requirements-worker.txt" in dev_requirements
    for package in ("fastapi==", "sqlalchemy==", "redis==", "boto3==", "cryptography==", "httpx=="):
        assert package in api_requirements, f"API runtime dependency {package} is missing"

    dockerfile = (REPO_ROOT / "infrastructure/Dockerfile.api").read_text()
    assert "requirements-worker.txt" in dockerfile
    assert _vercel()["services"]["api"]["root"] == "apps/api"


def test_api_runtime_defaults_do_not_start_media_workers_inline():
    assert Settings.model_fields["worker_inline"].default is False
    assert Settings.model_fields["worker_external"].default is False


def test_compose_defines_the_full_stack():
    services = _compose()["services"]
    for required in ("web", "api", "worker", "postgres", "redis", "minio", "processor"):
        assert required in services, f"{required} is missing from docker-compose.yml"


def test_api_and_worker_run_the_same_image():
    """Different images would let the API and its workers drift apart."""
    services = _compose()["services"]
    api = services["api"]
    worker = services["worker"]
    assert api["image"] == worker["image"]
    assert api["build"]["dockerfile"] == worker["build"]["dockerfile"]
    assert api["command"] != worker["command"], "the worker must not start the HTTP server"


def test_worker_does_not_run_the_api_inline():
    """A dedicated worker container must disable the in-process worker pool."""
    env = _compose()["services"]["worker"]["environment"]
    assert str(env["WORKER_INLINE"]).lower() == "false"


def test_every_compose_env_var_is_read_by_the_application():
    """An unknown key would be silently ignored, which is worse than failing."""
    # pydantic-settings matches case-insensitively, so DATABASE_URL maps to the
    # `database_url` field; compare in one normal form.
    declared = {name.lower() for name in Settings.model_fields}
    # Compose-only keys: consumed by the container runtime or the image, not Settings.
    runtime_only = {"postgres_password", "minio_port", "minio_console_port", "api_port", "web_port", "rust_log"}

    env = _compose()["services"]["api"]["environment"]
    unknown = {key for key in env if key.lower() not in declared and key.lower() not in runtime_only}
    assert not unknown, f"docker-compose.yml sets env vars Settings does not define: {sorted(unknown)}"


def test_compose_datastore_urls_point_at_compose_service_names():
    env = _compose()["services"]["api"]["environment"]
    assert "@postgres:5432" in env["DATABASE_URL"]
    assert env["REDIS_URL"].startswith("redis://redis:6379")
    assert env["S3_ENDPOINT"] == "http://minio:9000"
    assert env["PROCESSOR_URL"].startswith("http://processor:")


def test_dockerfiles_and_their_build_contexts_exist():
    services = _compose()["services"]
    for name, config in services.items():
        build = config.get("build")
        if not build:
            continue
        context = (REPO_ROOT / build["context"]).resolve()
        dockerfile = (context / build["dockerfile"]).resolve()
        assert context.is_dir(), f"{name}: build context {context} does not exist"
        assert dockerfile.is_file(), f"{name}: dockerfile {dockerfile} does not exist"


def test_api_transfers_media_through_object_storage_not_a_shared_local_volume():
    """Vercel/API and Docker workers may run on different hosts and disks."""
    services = _compose()["services"]
    api = services["api"]
    worker = services["worker"]
    processor = services["processor"]

    assert not any("media-work" in mount for mount in api.get("volumes", []))
    assert any("media-work" in mount for mount in worker["volumes"])
    # The optional Rust processor may share the worker's local scratch path in
    # Compose; that scratch volume is not the API-to-worker transfer mechanism.
    assert worker["volumes"] == processor["volumes"]
    assert api["environment"]["STORAGE_BACKEND"] == "s3"
    assert worker["environment"]["STORAGE_BACKEND"] == "s3"
    assert api["environment"]["S3_BUCKET"] == worker["environment"]["S3_BUCKET"]


def test_nginx_proxies_to_the_compose_api_service():
    config = (REPO_ROOT / "apps" / "web" / "nginx.conf").read_text()
    assert "server api:8000;" in config, "the upstream must match the compose service name"
    # The routes the browser depends on must all be proxied.
    for route in ("/api/", "/ws/", "/healthz", "/readyz"):
        assert f"location {route}" in config or f"location = {route}" in config


def test_nginx_keeps_the_spa_fallback_and_websocket_upgrade():
    config = (REPO_ROOT / "apps" / "web" / "nginx.conf").read_text()
    assert "try_files $uri $uri/ /index.html;" in config, "client-side routes would 404"
    assert "proxy_set_header Upgrade    $http_upgrade;" in config


def test_api_image_installs_media_tooling_the_pipeline_requires():
    dockerfile = (REPO_ROOT / "infrastructure" / "Dockerfile.api").read_text()
    # FFmpeg is the pipeline; the fonts are required for burned-in captions.
    for package in ("ffmpeg", "fonts-dejavu"):
        assert package in dockerfile, f"{package} missing from the API image"
    assert "USER clipforge" in dockerfile, "containers should not run as root"


def test_processor_image_is_built_from_the_rust_crate():
    compose = _compose()
    context = compose["services"]["processor"]["build"]["context"]
    assert context == "apps/processor/rust"
    manifest = (REPO_ROOT / "apps/processor/rust/Cargo.toml").read_text()
    assert 'name = "clipforge-processor"' in manifest
    assert 'path = "src/main.rs"' in manifest


def test_env_example_documents_the_variables_the_stack_needs():
    example = (REPO_ROOT / ".env.example").read_text()
    for key in (
        "DATABASE_URL",
        "REDIS_URL",
        "S3_ENDPOINT",
        "S3_ACCESS_KEY",
        "S3_SECRET_KEY",
        "S3_BUCKET",
        "JWT_SECRET",
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "ANTHROPIC_API_KEY",
        "WHISPER_MODEL",
        "FRONTEND_URL",
        "API_URL",
        "WEBSOCKET_URL",
    ):
        assert re.search(rf"^{key}=", example, re.MULTILINE), f"{key} is not documented in .env.example"


def _dockerfile_copy_sources(dockerfile: Path) -> list[tuple[str, int]]:
    """Return (source, line number) for every COPY that reads from the context.

    Copies with `--from=<stage>` read from another build stage, not the build
    context, so they are skipped.
    """
    entries: list[tuple[str, int]] = []
    for number, line in enumerate(dockerfile.read_text().splitlines(), start=1):
        stripped = line.strip()
        if not stripped.upper().startswith("COPY "):
            continue
        if "--from=" in stripped:
            continue
        parts = [piece for piece in stripped.split() if not piece.startswith("--")]
        # COPY <src>... <dest>
        for source in parts[1:-1]:
            entries.append((source, number))
    return entries


def test_every_dockerfile_copy_source_exists_in_its_build_context():
    """A COPY from a missing path is a build failure — and an easy one to typo."""
    problems: list[str] = []
    for name, config in _compose()["services"].items():
        build = config.get("build")
        if not build:
            continue
        context = (REPO_ROOT / build["context"]).resolve()
        dockerfile = (context / build["dockerfile"]).resolve()
        for source, line in _dockerfile_copy_sources(dockerfile):
            # A glob or a directory is acceptable; only flag unambiguously absent paths.
            if "*" in source or "?" in source:
                continue
            target = (context / source).resolve()
            if not target.exists():
                problems.append(f"{name}: {dockerfile.name}:{line} copies '{source}' which does not exist")
    assert not problems, "\n".join(problems)


def test_api_image_contains_the_migration_environment():
    """Alembic must ship in the image that runs migrations.

    Shipping only `app/` made `alembic upgrade head` impossible in the deployed
    container, which is exactly when it is needed.
    """
    dockerfile = (REPO_ROOT / "infrastructure/Dockerfile.api").read_text()
    assert "COPY apps/api/alembic.ini /srv/app/apps/api/alembic.ini" in dockerfile
    assert "COPY apps/api/alembic /srv/app/apps/api/alembic" in dockerfile
    # The root-level copies must stay gone: those paths no longer exist and a
    # dangling COPY fails the image build.
    assert "COPY alembic.ini /srv/app/alembic.ini" not in dockerfile
    assert (REPO_ROOT / "apps/api/alembic.ini").exists()
    assert (REPO_ROOT / "apps/api/alembic" / "env.py").exists()
    versions = list((REPO_ROOT / "apps/api/alembic" / "versions").glob("*.py"))
    assert versions, "no migration revisions were generated"
    requirements = (REPO_ROOT / "apps/api" / "requirements.txt").read_text().lower()
    assert "alembic" in requirements, "the image cannot migrate without the package"
