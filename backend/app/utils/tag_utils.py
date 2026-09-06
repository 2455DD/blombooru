import fnmatch
import time
from typing import Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import selectinload, Session

def resolve_aliases(db: Session, raw_names: List[str]) -> Dict[str, Tuple[str, str]]:
    """Build an alias lookup map for a list of (already lowercased) tag names."""
    from ..models import TagAlias

    if not raw_names:
        return {}

    aliases = db.query(TagAlias).filter(TagAlias.alias_name.in_(raw_names)).all()
    return {
        a.alias_name: (a.target_tag.name, a.target_tag.category)
        for a in aliases
    }

def expand_implications(db: Session, tag_set: Dict[int, object], implications: Optional[List[object]] = None) -> None:
    """Recursively expand tag implications into *tag_set*, mutating it in place."""
    from ..models import TagImplication

    if implications is None:
        implications = db.query(TagImplication).all()
    if not implications:
        return

    changed = True
    while changed:
        changed = False
        current_names = {t.name for t in tag_set.values()}

        for imp in implications:
            triggered = any(t.id in tag_set for t in imp.target_tags)
            if not triggered and imp.target_tag_patterns:
                triggered = any(
                    fnmatch.fnmatch(tag_name, pattern)
                    for tag_name in current_names
                    for pattern in imp.target_tag_patterns
                )

            if triggered:
                for implied_tag in imp.implied_tags:
                    if implied_tag.id not in tag_set:
                        tag_set[implied_tag.id] = implied_tag
                        changed = True

def resolve_implications(db: Session, tags: list[str], max_depth: int = 10) -> list[str]:
    """
    Recursively resolve all tag implications for a given list of tag names.
    Returns the names of all implied tags that were not in the original input.
    """
    if not tags:
        return []

    from ..models import Tag, TagImplication, blombooru_implication_targets
    from ..utils.logger import logger

    start_time = time.perf_counter()

    initial_names = {t.strip().lower() for t in tags if t.strip()}
    if not initial_names:
        return []

    active_names = set(initial_names)

    # Resolve database IDs for any known tags in the initial set
    existing_tags = db.execute(
        select(Tag.id, Tag.name).where(Tag.name.in_(active_names))
    ).all()
    active_ids = {t_id for t_id, _ in existing_tags}

    # Fetch standalone pattern rules (implications with wildcard patterns but no target tags)
    pattern_only_rules = db.execute(
        select(TagImplication)
        .where(
            TagImplication.target_tag_patterns.is_not(None),
            ~TagImplication.target_tags.any(),
        )
        .options(selectinload(TagImplication.implied_tags))
    ).scalars().all()

    applied_implications: set[int] = set()
    depth_reached = 0

    for depth in range(max_depth):
        depth_reached = depth
        newly_implied: list[Tag] = []

        # Evaluate pattern-only implications against current tag names
        for rule in pattern_only_rules:
            if rule.id in applied_implications:
                continue
            patterns = rule.target_tag_patterns or []
            if patterns and all(any(fnmatch.fnmatch(name, pat) for name in active_names) for pat in patterns):
                applied_implications.add(rule.id)
                newly_implied.extend(rule.implied_tags)

        # Evaluate target-tag implications targeting any current tag IDs
        if active_ids:
            stmt = (
                select(TagImplication)
                .join(
                    blombooru_implication_targets,
                    TagImplication.id == blombooru_implication_targets.c.implication_id,
                )
                .where(
                    blombooru_implication_targets.c.tag_id.in_(active_ids),
                )
                .options(
                    selectinload(TagImplication.target_tags),
                    selectinload(TagImplication.implied_tags),
                )
                .distinct(TagImplication.id)
            )
            if applied_implications:
                stmt = stmt.where(~TagImplication.id.in_(applied_implications))

            candidates = db.execute(stmt).scalars().all()

            for rule in candidates:
                if rule.id in applied_implications:
                    continue

                # Every target tag must be present in the active ID set
                if not all(t.id in active_ids for t in rule.target_tags):
                    continue

                # Any associated pattern constraints must also be satisfied
                patterns = rule.target_tag_patterns
                if patterns:
                    if not all(any(fnmatch.fnmatch(name, pat) for name in active_names) for pat in patterns):
                        continue

                applied_implications.add(rule.id)
                newly_implied.extend(rule.implied_tags)

        # Determine if any genuinely new tags were introduced in this pass
        new_names = {t.name.lower() for t in newly_implied if t.name.lower() not in active_names}
        new_ids = {t.id for t in newly_implied if t.id not in active_ids}

        if not new_names and not new_ids:
            break

        active_names.update(new_names)
        active_ids.update(new_ids)

    logger.debug(
        f"Implication expansion finished in {time.perf_counter() - start_time:.3f}s "
        f"(depth={depth_reached}, implied={len(active_names - initial_names)})"
    )

    return sorted(active_names - initial_names)
