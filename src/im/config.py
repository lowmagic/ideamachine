"""Segmentation config. Defaults here; override with a TOML file named by IM_CONFIG.

Every value that changes episode boundaries goes into stage_version, so a
config change is a version change and re-derives episodes.
"""

import os
import sys
import tomllib
from dataclasses import dataclass, fields

HEURISTIC = "heuristic-3"  # 3: episode identity covers how its speakers would be pseudonymized


@dataclass(frozen=True)
class SegmentConfig:
    gap_s: float = 60.0              # G: silence longer than this always splits
    mode_gap_s: float = 5.0          # a self/other switch splits only across a pause at least this long
    min_monologue_s: float = 30.0    # a run of my own speech this long becomes its own episode

    @property
    def stage_version(self) -> str:
        params = ";".join(f"{f.name}={getattr(self, f.name):g}" for f in fields(self))
        return f"segment/{HEURISTIC};{params}"


@dataclass(frozen=True)
class ExtractConfig:
    enabled: bool = True
    model: str = "claude-opus-5-5"
    effort: str = "medium"
    prompt: str = "v5.3"                 # extract.LIVE; a new one reads new speech, the rest waits for `im reread`
    monthly_cap_usd: float = 20.0        # every paid request stops once the month's egress spend reaches this
    max_episodes_per_run: int | None = None
    max_tokens: int | None = None        # a request's output budget, reasoning included (None: llm.MAX_TOKENS)


@dataclass(frozen=True)
class ModelConfig:
    """The model a stage asks, and its effort: [match], [digest], [connect]. Both go into the stage's version: what
    that stage did under the old one (the pairs the connection judge has judged) is held until `im reread`
    (im.reread); the matcher and the digest use it from then on. `max_tokens`, a request's output budget, isn't in the
    version (None: the stage's own)."""
    model: str = "claude-opus-5-5"
    effort: str = "low"
    max_tokens: int | None = None


@dataclass(frozen=True)
class ConnectConfig(ModelConfig):
    """[connect]: the connection judge's model and effort, and the most pairs `im run` asks about (None: every due
    pair; 0: only count them). The rest wait for the next run; the limit isn't in the version."""
    model: str = "openai/gpt-6.1-terra"   # Terra passed B4
    max_pairs_per_run: int | None = 500   # about $0.60 a run while that many are due (the backfill's prices)


@dataclass(frozen=True)
class LatticeConfig:
    enabled: bool = True
    embedding_model: str = "BAAI/bge-small-en-v1.5"   # local, CPU
    max_items_per_run: int | None = None


@dataclass(frozen=True)
class WeightConfig:
    """weight-1 (docs/design/m2-observations-candidates-ideas.md). Any change is a new formula version: its weights
    are written beside the old ones. Preview a change with `im tune`."""
    recurrence_minutes: float = 30.0   # two of my observations in one conversation this far apart count twice
    others: float = 0.25               # per observation by diarization or not me...
    others_cap: int = 4                # ...counting at most this many, so a crowd can't outweigh me
    canonical_link: float = 0.5        # per link to a canonical idea
    canonical: float = 2.0             # when the cluster is itself a canonical idea
    proposals_per_week: int = 10
    repropose_margin: float = 1.0      # a dismissed candidate comes back once its weight has grown this much

    @property
    def formula_version(self) -> str:
        params = ";".join(f"{f.name}={getattr(self, f.name):g}" for f in fields(self)
                          if f.name not in ("proposals_per_week", "repropose_margin"))
        return f"weight-1;{params}"


@dataclass(frozen=True)
class EgressConfig:
    # Zero data retention: every request goes only to endpoints that keep nothing, Claude's through OpenRouter (to
    # Bedrock or Vertex) since Anthropic's own API keeps requests for 30 days. The route isn't part of any version.
    zdr: bool = False


# Policy C's keys, retired with it (decision 0008, amended 2026-11-11): ignored, with a warning, until deleted.
RETIRED_EGRESS = ("my_names", "names", "public_names")
# extract.interests filled prompt v2's slot, and was retired with prompts v1 to v4 at the cutover (decision 0012):
# ignored, with a warning, until deleted.
RETIRED_EXTRACT = ("interests",)


@dataclass(frozen=True)
class Config:
    segment: SegmentConfig = SegmentConfig()
    extract: ExtractConfig = ExtractConfig()
    lattice: LatticeConfig = LatticeConfig()
    match: ModelConfig = ModelConfig()          # the lattice's matcher
    digest: ModelConfig = ModelConfig()
    connect: ConnectConfig = ConnectConfig()   # the connection judge
    weight: WeightConfig = WeightConfig()
    egress: EgressConfig = EgressConfig()


def load() -> Config:
    path = os.environ.get("IM_CONFIG")
    if not path:
        return Config()
    with open(path, "rb") as f:
        data = tomllib.load(f)
    if "triage" in data:   # the triage backends, retired with V1's routing, labels and evals: ignored until deleted
        print(f"[triage]: retired with routing; delete it from {path}", file=sys.stderr)
    if "themes" in data:   # the themes filer, proposer and describer, retired when themes became lenses (M5)
        print(f"[themes]: retired when themes became lenses; delete it from {path}", file=sys.stderr)
    egress = {k: tuple(v) if isinstance(v, list) else v for k, v in data.get("egress", {}).items()
              if k not in RETIRED_EGRESS}
    if retired := [k for k in RETIRED_EGRESS if k in data.get("egress", {})]:
        print(f"[egress] {', '.join(retired)}: retired with policy C (decision 0008); delete them from {path}",
              file=sys.stderr)
    extract = {k: v for k, v in data.get("extract", {}).items() if k not in RETIRED_EXTRACT}
    if retired := [k for k in RETIRED_EXTRACT if k in data.get("extract", {})]:
        print(f"[extract] {', '.join(retired)}: retired with prompts v1 to v4 (decision 0012); delete it from {path}",
              file=sys.stderr)
    models = {k: ModelConfig(**data.get(k, {})) for k in ("match", "digest")}
    models["connect"] = ConnectConfig(**data.get("connect", {}))
    return Config(segment=SegmentConfig(**data.get("segment", {})),
                  extract=ExtractConfig(**extract), lattice=LatticeConfig(**data.get("lattice", {})),
                  weight=WeightConfig(**data.get("weight", {})), egress=EgressConfig(**egress), **models)
