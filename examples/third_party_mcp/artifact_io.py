"""Compatibility import for the original example; one SDK implementation only."""
try:
    from tcode_artifacts import ArtifactIO
except ModuleNotFoundError as error:
    if error.name != 'tcode_artifacts':
        raise
    # Preserve running the example directly from a repository checkout.
    import sys
    from pathlib import Path
    source = Path(__file__).resolve().parents[2] / 'packages/artifact-sdk/src'
    if not (source / 'tcode_artifacts/__init__.py').is_file():
        raise
    sys.path.insert(0, str(source))
    try:
        from tcode_artifacts import ArtifactIO
    finally:
        sys.path.remove(str(source))

__all__ = ['ArtifactIO']
