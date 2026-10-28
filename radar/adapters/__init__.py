"""adapters - one module per ATS platform.

Every module in this package implements the same small contract:

    fetch_<platform>(target) -> [job(), ...]     required
    expand(target)           -> [target, ...]    optional, default [target]
    hydrate(job, kind)       -> None             optional, mutates in place

`fetch_*` is pass 1: cheap, title-and-location only, run across every board on
every sweep. `hydrate` is pass 2: one request per *finalist*, after the title
and location filters have cut thousands of postings down to a few hundred.
`expand` exists because some platforms have no "all jobs" view and have to be
swept once per search term.

Adapters may import `fetchcore`; they may NOT import `job_radar`. That is the
whole point of the split - 21 modules share a transport without any of them
being able to reach into the classifier, the scorer or the database. A hydrate
that needs a posting re-classified changes `job["location"]` and lets
job_radar.hydrate() re-derive the geo fields.

Adding a platform is therefore: write one module here, add two lines below.

Nothing here imports job_radar, and nothing here may: the dependency runs one
way, and selftest asserts it.
"""

import os

from . import amazon, ashby, avature, breezy, greenhouse, html, insightglobal
from . import jobbank, jobvite, lever, oracle, phenom, recruitee, rippling
from . import smartrecruiters, successfactors, taleo, teamtailor, ultipro
from . import workable, workday, wpjobs

_MODULES = {
    "greenhouse": greenhouse, "lever": lever, "ashby": ashby,
    "recruitee": recruitee, "rippling": rippling, "workday": workday,
    "phenom": phenom, "successfactors": successfactors, "oracle": oracle,
    "jobvite": jobvite, "html": html, "workable": workable,
    "smartrecruiters": smartrecruiters, "taleo": taleo, "wpjobs": wpjobs,
    "amazon": amazon, "avature": avature, "teamtailor": teamtailor,
    "ultipro": ultipro, "jobbank": jobbank, "insightglobal": insightglobal,
    "breezy": breezy,
}

ADAPTERS = {k: getattr(m, "fetch_" + k) for k, m in _MODULES.items()}
HYDRATORS = {k: m.hydrate for k, m in _MODULES.items() if hasattr(m, "hydrate")}
_EXPAND = {k: m.expand for k, m in _MODULES.items() if hasattr(m, "expand")}


def expand(src, tok):
    """Fan one target out into the task list for its platform."""
    fn = _EXPAND.get(src)
    return fn(tok) if fn else [tok]


# The package's own surface. Anything platform-specific is reached through its
# module - `from adapters import successfactors` then `successfactors.
# looks_like(html)` - so that a caller's import says which platform it depends
# on. WD_TRUNCATED is here because the sweep report is written by job_radar and
# the truncation it reports is detected by the adapter.
__all__ = ["ADAPTERS", "HYDRATORS", "WD_TRUNCATED", "expand", "platform"]

from .workday import WD_TRUNCATED                                  # noqa: E402,F401


def platform(name):
    """The module that handles `name`, or None. For callers that hold a
    platform string rather than an import."""
    return _MODULES.get(name)


# A module that is importable but not wired in would silently drop every board
# on its platform - the failure mode that costs a day to notice. Fail at import
# instead. (Keep this last: it checks what everything above built.)
_FILES = {f[:-3] for f in os.listdir(os.path.dirname(os.path.abspath(__file__)))
          if f.endswith(".py") and not f.startswith("_")}
if _FILES != set(_MODULES):
    raise ImportError("adapters: modules on disk do not match the registry: "
                      f"unwired={sorted(_FILES - set(_MODULES))} "
                      f"missing={sorted(set(_MODULES) - _FILES)}")
