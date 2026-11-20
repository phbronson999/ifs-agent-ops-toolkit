from ifs_agent_ops.operations import service
from ifs_agent_ops.workspace.discovery import find_package
from ifs_agent_ops.workspace.package import PackageError


async def draft(package: str) -> str:
    try:
        pkg = find_package(package)
        report, skipped = service.draft(pkg)
        return service.draft_summary(pkg, report, skipped)
    except (PackageError, FileNotFoundError) as exc:
        return f"error: {exc}"


async def list_operations(package: str) -> str:
    try:
        return service.list_summary(find_package(package))
    except PackageError as exc:
        return f"error: {exc}"


async def get_operation(package: str, operation_id: str) -> str:
    try:
        return service.show(find_package(package), operation_id)
    except (PackageError, KeyError) as exc:
        return f"error: {exc}"
