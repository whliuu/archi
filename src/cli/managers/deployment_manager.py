import os
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Tuple

import yaml

from src.cli.utils.command_runner import CommandRunner
from src.utils.logging import get_logger

logger = get_logger(__name__)

class DeploymentError(Exception):
    """Custom exception for deployment failures"""
    def __init__(self, message: str, exit_code: int, stderr: str = None):
        self.exit_code = exit_code
        self.stderr = stderr
        super().__init__(message)

class DeploymentManager:
    """Manages container deployment using Compose"""
    
    def __init__(self, use_podman: bool = False):
        self.use_podman = use_podman
        self.compose_tool = "podman compose" if use_podman else "docker compose"
    
    def start_deployment(self, deployment_dir: Path) -> None:
        """Start the deployment using compose"""
        compose_file = deployment_dir / "compose.yaml"
        
        if not compose_file.exists():
            raise FileNotFoundError(f"Compose file not found: {compose_file}")
        
        logger.info(f"Starting compose deployment from {deployment_dir}")
        logger.info(f"Using compose file: {compose_file}")
        logger.info(f"(This might take a minute...)")
        
        # Validate compose file syntax first
        try:
            self._validate_compose_file(compose_file)
        except Exception as e:
            raise DeploymentError(f"Invalid compose file: {e}", 1)
        
        flags = os.environ.get("ARCHI_COMPOSE_UP_FLAGS", "--build --force-recreate --always-recreate-deps")

        # Rootless podman's `depends_on` support is unreliable (podman-compose /
        # podman start fail with "depends on container ... not found in input
        # list" even for a single `up -d`), so the compose template omits
        # depends_on when --podman is used and we replicate the ordering here:
        # postgres -> (wait healthy) -> config-seed -> (wait completed) -> rest.
        if self.use_podman:
            self._start_deployment_staged(deployment_dir, compose_file, flags)
            return

        compose_cmd = f"{self.compose_tool} -f {compose_file} up -d {flags}"

        try:
            stdout, stderr, exit_code = CommandRunner.run_streaming(compose_cmd, cwd=deployment_dir)

            if exit_code != 0:
                error_msg = f"Deployment failed with exit code {exit_code}"
                if stderr.strip():
                    error_msg += f"\nError output:\n{stderr}"
                raise DeploymentError(error_msg, exit_code, stderr)

            logger.info("Deployment started successfully")

        except KeyboardInterrupt:
            logger.warning("Deployment interrupted by user")
            raise
        except subprocess.SubprocessError as e:
            raise DeploymentError(f"Failed to execute compose command: {e}", getattr(e, 'returncode', 1))

    def _start_deployment_staged(self, deployment_dir: Path, compose_file: Path, flags: str) -> None:
        """Bring services up one at a time, in dependency order, for podman."""
        with open(compose_file, "r") as f:
            compose_data = yaml.safe_load(f) or {}
        services = compose_data.get("services") or {}

        def up(service_name: str) -> None:
            cmd = f"{self.compose_tool} -f {compose_file} up -d {flags} {service_name}"
            stdout, stderr, exit_code = CommandRunner.run_streaming(cmd, cwd=deployment_dir)
            if exit_code != 0:
                error_msg = f"Failed to start '{service_name}' with exit code {exit_code}"
                if stderr.strip():
                    error_msg += f"\nError output:\n{stderr}"
                raise DeploymentError(error_msg, exit_code, stderr)

        try:
            if "postgres" in services:
                logger.info("Starting postgres (podman staged startup)...")
                up("postgres")
                container_name = services["postgres"].get("container_name", "postgres")
                self._wait_for_healthy(container_name)

            if "config-seed" in services:
                logger.info("Running config-seed (podman staged startup)...")
                up("config-seed")
                container_name = services["config-seed"].get("container_name", "config-seed")
                self._wait_for_exit(container_name)

            remaining = [name for name in services if name not in ("postgres", "config-seed")]
            for service_name in remaining:
                logger.info(f"Starting {service_name} (podman staged startup)...")
                up(service_name)

            logger.info("Deployment started successfully")
        except KeyboardInterrupt:
            logger.warning("Deployment interrupted by user")
            raise

    def _wait_for_healthy(self, container_name: str, timeout: int = 120, interval: int = 3) -> None:
        deadline = time.monotonic() + timeout
        cmd = f"podman inspect --format {{{{.State.Health.Status}}}} {container_name}"
        while time.monotonic() < deadline:
            stdout, stderr, exit_code = CommandRunner.run_simple(cmd)
            status = stdout.strip()
            if status == "healthy":
                return
            if status == "unhealthy":
                raise DeploymentError(f"Container '{container_name}' became unhealthy", 1, stderr)
            time.sleep(interval)
        raise DeploymentError(f"Timed out waiting for '{container_name}' to become healthy", 1)

    def _wait_for_exit(self, container_name: str, timeout: int = 120, interval: int = 3) -> None:
        deadline = time.monotonic() + timeout
        status_cmd = f"podman inspect --format {{{{.State.Status}}}} {container_name}"
        exit_cmd = f"podman inspect --format {{{{.State.ExitCode}}}} {container_name}"
        while time.monotonic() < deadline:
            stdout, stderr, exit_code = CommandRunner.run_simple(status_cmd)
            status = stdout.strip()
            if status == "exited":
                stdout, stderr, exit_code = CommandRunner.run_simple(exit_cmd)
                if stdout.strip() != "0":
                    raise DeploymentError(f"Container '{container_name}' exited with code {stdout.strip()}", 1, stderr)
                return
            time.sleep(interval)
        raise DeploymentError(f"Timed out waiting for '{container_name}' to complete", 1)
    
    def stop_deployment(self, deployment_dir: Path) -> None:
        """Stop the deployment"""
        compose_file = deployment_dir / "compose.yaml"
        
        if not compose_file.exists():
            raise FileNotFoundError(f"Compose file not found: {compose_file}")
        
        logger.info("Stopping deployment")
        
        compose_cmd = f"{self.compose_tool} -f {compose_file} down"
        
        try:
            stdout, stderr, exit_code = CommandRunner.run_streaming(compose_cmd, cwd=deployment_dir)
            
            if exit_code != 0:
                logger.warning(f"Stop command completed with exit code {exit_code}")
                if stderr.strip():
                    logger.warning(f"Warning output:\n{stderr}")
            else:
                logger.info("Deployment stopped successfully")
                
        except subprocess.SubprocessError as e:
            raise DeploymentError(f"Failed to stop deployment: {e}", getattr(e, 'returncode', 1))

    def restart_service(self, deployment_dir: Path, service_name: str, build: bool = True,
                        no_deps: bool = True, force_recreate: bool = True) -> None:
        """Restart a specific service using compose"""
        compose_file = deployment_dir / "compose.yaml"

        if not compose_file.exists():
            raise FileNotFoundError(f"Compose file not found: {compose_file}")

        logger.info(f"Restarting service '{service_name}'")

        try:
            self._validate_compose_file(compose_file)
        except Exception as e:
            raise DeploymentError(f"Invalid compose file: {e}", 1)

        flags = []
        if no_deps:
            flags.append("--no-deps")
        if build:
            flags.append("--build")
        if force_recreate:
            flags.append("--force-recreate")

        flags_str = " ".join(flags)
        compose_cmd = f"{self.compose_tool} -f {compose_file} up -d {flags_str} {service_name}".strip()

        try:
            stdout, stderr, exit_code = CommandRunner.run_streaming(compose_cmd, cwd=deployment_dir)

            if exit_code != 0:
                error_msg = f"Restart failed with exit code {exit_code}"
                if stderr.strip():
                    error_msg += f"\nError output:\n{stderr}"
                raise DeploymentError(error_msg, exit_code, stderr)

            logger.info(f"Service '{service_name}' restarted successfully")
        except KeyboardInterrupt:
            logger.warning("Restart interrupted by user")
            raise
        except subprocess.SubprocessError as e:
            raise DeploymentError(f"Failed to restart service: {e}", getattr(e, 'returncode', 1))

    def has_service(self, deployment_dir: Path, service_name: str) -> bool:
        compose_file = deployment_dir / "compose.yaml"
        if not compose_file.exists():
            raise FileNotFoundError(f"Compose file not found: {compose_file}")
        self._validate_compose_file(compose_file)
        import yaml
        with open(compose_file, 'r') as f:
            compose_data = yaml.safe_load(f) or {}
        services = compose_data.get("services") or {}
        return service_name in services

    def run_service_once(self, deployment_dir: Path, service_name: str, build: bool = True,
                         no_deps: bool = True) -> None:
        """Run a one-shot compose service and remove the container when it exits."""
        compose_file = deployment_dir / "compose.yaml"

        if not compose_file.exists():
            raise FileNotFoundError(f"Compose file not found: {compose_file}")

        logger.info(f"Running one-shot service '{service_name}'")

        try:
            self._validate_compose_file(compose_file)
        except Exception as e:
            raise DeploymentError(f"Invalid compose file: {e}", 1)

        flags = ["--rm"]
        if no_deps:
            flags.append("--no-deps")
        if build:
            flags.append("--build")

        flags_str = " ".join(flags)
        compose_cmd = f"{self.compose_tool} -f {compose_file} run {flags_str} {service_name}".strip()

        try:
            stdout, stderr, exit_code = CommandRunner.run_streaming(compose_cmd, cwd=deployment_dir)

            if exit_code != 0:
                error_msg = f"One-shot service '{service_name}' failed with exit code {exit_code}"
                if stderr.strip():
                    error_msg += f"\nError output:\n{stderr}"
                raise DeploymentError(error_msg, exit_code, stderr)

            logger.info(f"One-shot service '{service_name}' completed successfully")
        except KeyboardInterrupt:
            logger.warning("One-shot service interrupted by user")
            raise
        except subprocess.SubprocessError as e:
            raise DeploymentError(f"Failed to run one-shot service: {e}", getattr(e, 'returncode', 1))
    
    def delete_deployment(self, deployment_name: str, remove_images: bool = False, 
                         remove_volumes: bool = False, remove_files: bool = True) -> None:
        """Delete a deployment and optionally clean up resources"""
        # Determine deployment directory
        import os

        from src.cli.managers.volume_manager import VolumeManager
        ARCHI_DIR = os.environ.get('ARCHI_DIR', os.path.join(os.path.expanduser('~'), ".archi"))
        deployment_dir = Path(ARCHI_DIR) / f"archi-{deployment_name}"
        
        if deployment_dir.exists():
            # Stop deployment first
            try:
                self.stop_deployment(deployment_dir)
            except Exception as e:
                logger.warning(f"Could not stop deployment: {e}")
            
            # Remove images if requested
            if remove_images:
                try:
                    self._remove_images(deployment_dir)
                except Exception as e:
                    logger.warning(f"Could not remove images: {e}")
            
            # Remove volumes if requested
            if remove_volumes:
                try:
                    volume_manager = VolumeManager(self.use_podman)
                    volume_manager.remove_deployment_volumes(deployment_name, force=True)
                except Exception as e:
                    logger.warning(f"Could not remove volumes: {e}")
            
            # Remove files if requested
            if remove_files:
                try:
                    import shutil
                    shutil.rmtree(deployment_dir)
                    logger.info(f"Removed deployment directory: {deployment_dir}")
                except Exception as e:
                    logger.warning(f"Could not remove deployment directory: {e}")
        else:
            logger.info(f"Deployment directory does not exist: {deployment_dir}. Cannot take down deployment.")
    
    def _validate_compose_file(self, compose_file: Path) -> None:
        """Validate compose file syntax"""
        try:
            import yaml
            with open(compose_file, 'r') as f:
                yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise ValueError(f"YAML syntax error in compose file: {e}")
        except Exception as e:
            raise ValueError(f"Could not read compose file: {e}")
    
    def _remove_images(self, deployment_dir: Path) -> None:
        """Remove images associated with the deployment"""
        compose_file = deployment_dir / "compose.yaml"
        if not compose_file.exists():
            return
            
        # Get list of images
        images_cmd = f"{self.compose_tool} -f {compose_file} images -q"
        try:
            stdout, stderr, exit_code = CommandRunner.run_streaming(images_cmd, cwd=deployment_dir)
            if exit_code == 0 and stdout.strip():
                # Remove images
                tool = "podman" if self.use_podman else "docker"
                for image_id in stdout.strip().split('\n'):
                    if image_id.strip():
                        remove_cmd = f"{tool} rmi {image_id.strip()}"
                        CommandRunner.run_streaming(remove_cmd, cwd=deployment_dir)
                        logger.info(f"Removing image with id: {image_id}")
        except Exception as e:
            logger.warning(f"Could not remove images: {e}")
