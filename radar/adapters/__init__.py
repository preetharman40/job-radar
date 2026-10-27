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
"""

import os

from . import amazon, ashby, avature, greenhouse, html, insightglobal, jobbank
from . import jobvite, lever, oracle, phenom, recruitee, rippling
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
}

ADAPTERS = {k: getattr(m, "fetch_" + k) for k, m in _MODULES.items()}
HYDRATORS = {k: m.hydrate for k, m in _MODULES.items() if hasattr(m, "hydrate")}
_EXPAND = {k: m.expand for k, m in _MODULES.items() if hasattr(m, "expand")}


def expand(src, tok):
    """Fan one target out into the task list for its platform."""
    fn = _EXPAND.get(src)
    return fn(tok) if fn else [tok]


# Names the rest of the radar reads. Re-exported here so callers need one
# import, not one per platform.
from .workday import WD_CANADA, WD_QUERIES, WD_TENANT_BURST, WD_TRUNCATED  # noqa: E402,F401
from .workday import _wd_country_facet                                    # noqa: E402,F401
from .successfactors import SF_QUERIES, SF_ROW, SF_TILE                   # noqa: E402,F401
from .oracle import ORC_QUERIES                                           # noqa: E402,F401
from .jobvite import JV_QUERIES                                           # noqa: E402,F401
from .avature import AV_QUERIES                                           # noqa: E402,F401
from .ashby import ASHBY_ONE, _ashby_wait                                 # noqa: E402,F401

# A module that is importable but not wired in would silently drop every board
# on its platform - the failure mode that costs a day to notice. Fail at import
# instead. (Keep this last: it checks what everything above built.)
_FILES = {f[:-3] for f in os.listdir(os.path.dirname(os.path.abspath(__file__)))
          if f.endswith(".py") and not f.startswith("_")}
if _FILES != set(_MODULES):
    raise ImportError("adapters: modules on disk do not match the registry: "
                      f"unwired={sorted(_FILES - set(_MODULES))} "
                      f"missing={sorted(set(_MODULES) - _FILES)}")
