# Job Agent v2 - Mission & Vision

**Version**: 2.0
**Status**: Design Phase
**Last Updated**: May 2026
**Architecture**: Zero-LLM, Local-First, ATS-Optimized

---

## Executive Summary

Job Agent v2 is a complete architectural redesign that eliminates all LLM API dependencies while achieving **90%+ matching quality** through a sophisticated ensemble of local NLP techniques. This transformation reduces operational costs from **$72-180/month to $0/month** while adding ATS-beating capabilities that help candidates rank higher in applicant tracking systems.

---

## Mission Statement

**Build the most effective, cost-free job search automation system that outperforms both human recruiters and ATS algorithms at matching candidates to opportunities.**

### Core Objectives

1. **Zero API Cost** - All processing runs locally with no external dependencies
2. **ATS Supremacy** - Reverse-engineer ATS scoring to optimize candidate ranking
3. **Semantic Understanding** - Match skills contextually, not just by keyword
4. **Staff Engineer Quality** - Architecture that scales, maintains, and evolves gracefully
5. **Backward Compatible** - Seamless migration from v1 without data loss

---

## Problem Analysis

### Current State (v1)

| Aspect | v1 Implementation | Cost/Issue |
|--------|------------------|------------|
| Job Evaluation | Claude API | $0.002-0.005/job = $72-180/month |
| Semantic Matching | LLM-based | API rate limits, latency |
| Skills Extraction | LLM inference | Token overhead |
| ATS Optimization | None | Candidates don't rank well |
| Failure Mode | API outage = system down | Single point of failure |

### Target State (v2)

| Aspect | v2 Implementation | Cost/Benefit |
|--------|------------------|--------------|
| Job Evaluation | Local ML ensemble | $0/month |
| Semantic Matching | Sentence-BERT (local) | 500ms latency, unlimited |
| Skills Extraction | SkillNER + ESCO taxonomy | Deterministic, fast |
| ATS Optimization | Rule-based ATS simulator | Beat 80% of applicants |
| Failure Mode | Graceful degradation | Multiple fallback layers |

---

## Success Metrics

### Quality Metrics

| Metric | v1 Baseline | v2 Target | Measurement |
|--------|-------------|-----------|-------------|
| Match Accuracy | 100% (LLM gold standard) | 90%+ | Human evaluation on 100 jobs |
| Skills Extraction Precision | ~95% (LLM) | 85%+ | Manual annotation comparison |
| False Positive Rate | ~5% | <10% | Jobs marked APPLY that shouldn't be |
| False Negative Rate | ~5% | <5% | Good jobs marked SKIP |

### Performance Metrics

| Metric | v1 | v2 Target | Improvement |
|--------|-----|-----------|-------------|
| Cost per 1000 jobs | $2-5 | $0 | 100% reduction |
| Evaluation latency | 3-5s/job | <1s/job | 3-5x faster |
| Throughput | 50 jobs/15min | 50 jobs/2min | 7.5x faster |
| Offline capability | No | Yes | 100% uptime |

### ATS Optimization Metrics

| Metric | Without Optimization | With v2 | Target |
|--------|---------------------|---------|--------|
| ATS Score (simulated) | 40-60% | 75-90% | Top 20% of applicants |
| Keyword coverage | Unknown | 85%+ | Tracked per job |
| Section structure score | Unknown | 100% | Standard ATS headers |

---

## Architectural Principles

### 1. Ensemble Over Monolith

**Principle**: Combine multiple weak signals into a strong prediction.

```
Final Score = w1(SBERT) + w2(BM25) + w3(Skills) + w4(Level) + w5(Rules)
```

**Rationale**: No single algorithm matches LLM quality. Ensemble of 5+ signals achieves comparable results with deterministic behavior.

### 2. Progressive Enhancement

**Principle**: System works with minimal components, improves with optional ones.

```
Layer 1: Rule-based filtering (always works)
Layer 2: TF-IDF matching (fast, basic)
Layer 3: SBERT semantic (quality boost)
Layer 4: Skills taxonomy (precision boost)
Layer 5: ATS simulation (optimization)
```

**Rationale**: If SBERT model fails to load, system falls back to TF-IDF. Never fully breaks.

### 3. Explain Everything

**Principle**: Every score must be decomposable and explainable.

```python
{
    "overall_score": 78,
    "breakdown": {
        "semantic_similarity": 82,  # SBERT
        "keyword_match": 75,        # BM25
        "skills_overlap": 80,       # 8/10 skills matched
        "level_fit": 70,            # Senior vs Principal
        "deal_breakers": 100        # No blockers
    },
    "explanation": "Strong semantic match. Missing: Terraform, Pulumi."
}
```

**Rationale**: Users need to trust scores. Explainability builds trust.

### 4. Cache Aggressively

**Principle**: Compute once, reuse forever.

```
CV Embeddings: Cached on first load (one-time)
Skills Taxonomy: Pre-loaded in memory
Job Embeddings: Stored in DB with job
Section Templates: Parsed once per CV update
```

**Rationale**: Most computation is redundant. CV doesn't change between jobs.

### 5. ATS-First Design

**Principle**: Optimize for how ATS systems actually work, not how they should work.

```
ATS Reality:
- Keyword density matters (2-4% optimal)
- Section headers must match regex patterns
- Recent experience weighted higher
- Skills section scanned first
- Synonym matching is limited
```

**Rationale**: Understanding ATS algorithms lets us game them ethically.

---

## Scope Definition

### In Scope (v2.0)

| Feature | Priority | Description |
|---------|----------|-------------|
| SBERT Matching | P0 | Sentence embeddings for semantic similarity |
| BM25/TF-IDF | P0 | Keyword matching for precision |
| Skills Extraction | P0 | SkillNER + custom taxonomy |
| Level Detection | P0 | Junior/Mid/Senior/Principal classification |
| Deal-breaker Filter | P0 | Fast rejection of non-starters |
| ATS Score Simulation | P1 | Predict ATS ranking |
| CV Gap Analysis | P1 | Identify missing skills per job |
| Ensemble Scoring | P0 | Weighted combination of signals |
| Explanation Generator | P1 | Human-readable match reasoning |
| Migration from v1 | P0 | Zero-downtime database migration |

### Out of Scope (Future)

| Feature | Target Version | Reason for Deferral |
|---------|---------------|---------------------|
| Fine-tuned SBERT | v2.1 | Requires training data collection |
| Interview Prep | v3.0 | Different feature domain |
| Salary Prediction | v3.0 | Requires external data sources |
| Multi-language | v2.2 | English-first simplifies v2.0 |
| Cloud Deployment | v3.0 | Local-first is design principle |

---

## Edge Cases & Failure Modes

### Edge Case Analysis

| Edge Case | Likelihood | Impact | Mitigation |
|-----------|------------|--------|------------|
| Job description too short (<50 words) | 5% | Low score accuracy | Use title + company as fallback |
| Non-English job posting | 10% | SBERT degradation | Detect language, skip or warn |
| CV not loaded | Rare | System crash | Mandatory validation on startup |
| Skills not in taxonomy | 30% | Miss specialized skills | Fuzzy matching + custom additions |
| ATS headers non-standard | 20% | Wrong section parsing | Multiple regex patterns |
| Job level ambiguous | 40% | Wrong level_fit score | Use multiple signals (YoE, keywords) |

### Failure Mode Handling

```python
# Graceful degradation strategy
def score_job(job, cv):
    scores = {}

    # Layer 1: Always works
    scores['rules'] = apply_deal_breakers(job, cv)
    if scores['rules'] == 0:
        return quick_reject(job)

    # Layer 2: Fast fallback
    try:
        scores['tfidf'] = tfidf_similarity(job, cv)
    except Exception:
        scores['tfidf'] = 50  # neutral default

    # Layer 3: Quality boost (may fail on low memory)
    try:
        scores['sbert'] = sbert_similarity(job, cv)
    except MemoryError:
        scores['sbert'] = scores['tfidf']  # fallback

    # Layer 4: Precision boost
    try:
        scores['skills'] = skills_overlap(job, cv)
    except TaxonomyNotLoaded:
        scores['skills'] = scores['tfidf']

    return weighted_ensemble(scores)
```

---

## Migration Strategy

### Phase 1: Parallel Running (Week 1-2)
- Deploy v2 scorer alongside v1
- Log both scores for same jobs
- Compare accuracy metrics
- No user-facing changes

### Phase 2: Shadow Mode (Week 3)
- v2 becomes primary scorer
- v1 runs as backup/validation
- Alert on significant divergence
- Users see v2 scores

### Phase 3: Full Migration (Week 4)
- Remove v1 Claude API calls
- Archive v1 code
- Update documentation
- Delete API key from config

### Database Migration

```sql
-- New columns for v2
ALTER TABLE jobs ADD COLUMN sbert_score FLOAT DEFAULT 0.0;
ALTER TABLE jobs ADD COLUMN bm25_score FLOAT DEFAULT 0.0;
ALTER TABLE jobs ADD COLUMN skills_score FLOAT DEFAULT 0.0;
ALTER TABLE jobs ADD COLUMN ats_score FLOAT DEFAULT 0.0;
ALTER TABLE jobs ADD COLUMN matched_skills TEXT;  -- JSON array
ALTER TABLE jobs ADD COLUMN missing_skills TEXT;  -- JSON array
ALTER TABLE jobs ADD COLUMN score_breakdown TEXT; -- JSON object

-- Preserve v1 scores for comparison
ALTER TABLE jobs ADD COLUMN v1_overall_score FLOAT;
UPDATE jobs SET v1_overall_score = overall_score WHERE overall_score > 0;
```

---

## Risk Assessment

| Risk | Probability | Impact | Mitigation |
|------|-------------|--------|------------|
| SBERT model too slow on CPU | Medium | User frustration | Batch processing + caching |
| Quality drop vs LLM | Medium | Bad job recommendations | Extensive A/B testing |
| Skills taxonomy incomplete | High | Missed matches | Continuous expansion |
| Memory issues with embeddings | Low | Crashes | Lazy loading + memory mapping |
| False confidence in ATS gaming | Medium | Over-optimization | Clear documentation of limits |
| BM25 preprocessing mismatch | Medium | Poor keyword matching | Consistent preprocessing pipeline |
| Level detection ambiguity | High (40%) | Wrong level_fit score | Confidence-weighted scoring |
| Ensemble weight miscalibration | Medium | Systematic bias | Calibration framework with LLM baseline |

### Detailed Risk Mitigations (Verified Against Official Documentation)

#### R1: SBERT Model Quality Degradation

**Problem**: `all-MiniLM-L6-v2` trained on general text may miss technical job semantics.

**Verified Facts** (source: huggingface.co/sentence-transformers):
- Max input: 256 word pieces (truncates beyond)
- Dimensions: 384
- Speed: ~14,000 sent/sec GPU, ~150 sent/sec CPU
- Training: 1.17B general sentence pairs

**Verified Mitigation**:
1. Use `msmarco-MiniLM-L6-cos-v5` for asymmetric search (query vs document) - SBERT docs recommend this for short-query-to-long-document matching
2. Truncate job descriptions with priority: requirements > responsibilities > description
3. Monitor low-confidence scores; fallback to `all-mpnet-base-v2` (768 dims, higher quality) or LLM when confidence < 30%

**Implementation**: See `analyzer/sbert_matcher.py` - `AdaptiveSBERTMatcher` class

#### R2: Skills Taxonomy Incompleteness

**Problem**: SkillNER's SKILL_DB misses modern tech (LangChain, Bun, Backstage).

**Verified Facts** (source: github.com/AnasAito/SkillNER):
> "In rule-based mode, the system is not able to detect a skill that is not present in the skill set."

**SKILL_DB Structure** (verified from source):
```python
{
    "skill_id": {
        "skill_len": int,           # 1 for single-word, >1 for phrases
        "high_surface_forms": list,  # Primary forms
        "low_surface_forms": list,   # Variations
        "match_on_tokens": bool
    }
}
```

**Verified Mitigation**:
1. Create `config/custom_skills.py` with modern tech skills following SKILL_DB structure
2. Merge custom DB with base: `merged = SKILL_DB.copy(); merged.update(CUSTOM_SKILLS_DB)`
3. When extraction count < expected (word_count / 100), trigger LLM fallback for skill extraction

**Implementation**: See `analyzer/skill_extractor.py` - `HybridSkillExtractor` class

#### R3: BM25 Preprocessing Mismatch

**Problem**: rank-bm25 does no preprocessing; inconsistent preprocessing breaks matching.

**Verified Facts** (source: github.com/dorianbrown/rank_bm25):
> "This package doesn't do any text preprocessing."
> "Apply the same preprocessing steps we did to the documents in order to have an apples-to-apples comparison."

**Verified Mitigation**:
1. Single `BM25Preprocessor` class used for BOTH corpus and queries
2. Preserve technical terms (c++, ci/cd, k8s) during preprocessing
3. Remove tech-specific stopwords (experience, years, required)

**Implementation**: See `analyzer/bm25_matcher.py` - `BM25Preprocessor` class

#### R4: Memory Management

**Problem**: Multiple large models may exceed available RAM.

**Verified Facts** (source: spacy.io/models):
- `en_core_web_sm`: 46 MB (no vectors)
- `en_core_web_md`: 74 MB (20k vectors)
- `en_core_web_lg`: 575 MB (500k vectors)

**Verified Mitigation** (source: spacy.io/usage):
> "exclude - The component isn't loaded at all, saving memory since its data isn't retrieved from disk."

1. Use `en_core_web_sm` (46MB) instead of `lg` (575MB)
2. Load with `exclude=["parser"]` - not needed for skill extraction
3. Lazy load all models via `ModelManager` singleton
4. Monitor memory with `psutil`; warn at < 1GB available

**Implementation**: See `analyzer/model_manager.py` - `ModelManager` class

#### R5: Level Detection Ambiguity

**Problem**: 40% of jobs have unclear level indicators.

**Verified Mitigation**:
1. Multi-signal detection: YoE regex (90% confidence), title keywords (85%), body keywords (60%)
2. Aggregate signals with confidence weighting
3. When confidence < 50%, move score toward neutral (50): `score = 50 + (base - 50) * (confidence / 100)`
4. Never let low-confidence level detection heavily penalize good semantic matches

**Implementation**: See `analyzer/level_detector.py` - `LevelDetector` class

#### R6: Ensemble Weight Calibration

**Problem**: Default weights (35/15/15/25/10) are assumptions.

**Verified Mitigation**:
1. Score 100 jobs with LLM (v1) as ground truth
2. Grid search over weight combinations (0.0 to 0.4 in 0.05 steps)
3. Optimize for: MAE < 10 points, correlation > 0.8, action agreement > 80%
4. Store calibrated weights in `config/scoring_weights.py`

**Implementation**: See `analyzer/weight_calibrator.py` - `WeightCalibrator` class

---

## Hybrid Architecture: LLM Fallback Strategy

**Principle**: Local-first with intelligent LLM fallback for edge cases.

### When to Fallback to LLM

| Condition | Detection | Action |
|-----------|-----------|--------|
| Short description | < 50 words | Fallback to LLM |
| Non-English | < 3 common English words in first 100 | Fallback or skip with warning |
| Low confidence | 3+ components score < 40 | Fallback to LLM |
| Missing skills | > 80% of job skills not matched | Fallback for skill extraction |
| Ambiguous level | Level confidence < 30% | Fallback to LLM |
| User request | `force_llm=True` | Always use LLM |

### Fallback Implementation

```python
# Check for ANTHROPIC_API_KEY at startup
llm_available = bool(os.getenv("ANTHROPIC_API_KEY"))

# In scorer
if should_fallback and llm_available:
    return score_with_llm(job)
elif should_fallback:
    # No LLM available - return local result with warning
    result.confidence = "low"
    result.warning = "LLM fallback unavailable"
    return result
else:
    return score_locally(job)
```

### LLM Model Selection for Fallback

| Use Case | Model | Cost | Latency |
|----------|-------|------|---------|
| Skill extraction | claude-3-haiku | ~$0.0001/job | ~500ms |
| Full scoring | claude-3-haiku | ~$0.0003/job | ~800ms |
| High-value jobs | claude-3-5-sonnet | ~$0.003/job | ~1.5s |

**Implementation**: See `analyzer/hybrid_scorer.py` - `HybridScorer` class

---

## Edge Cases & Resolution

| Edge Case | Likelihood | Detection | Resolution |
|-----------|------------|-----------|------------|
| Job description < 50 words | 5% | `len(desc.split()) < 50` | LLM fallback or use title+company only |
| Non-English posting | 10% | Common word overlap < 3 | LLM fallback or skip with warning |
| CV not loaded | Rare | Startup validation | Fail fast with clear error message |
| Skills not in taxonomy | 30% | Extraction count < expected | Custom skills DB + LLM fallback |
| ATS headers non-standard | 20% | Regex pattern failures | Multiple fallback patterns |
| Job level ambiguous | 40% | Confidence < 50% | Weight reduction, neutral score |
| SBERT token overflow | 15% | Word count > 200 | Priority truncation (requirements first) |
| Memory exhaustion | Rare | `psutil.virtual_memory().available < 512MB` | Lazy loading, smaller models |
| BM25 zero matches | 5% | Empty keyword intersection | Fallback to TF-IDF score |
| All components low | 3% | 3+ scores < 40 | LLM fallback |
| API rate limits | Rare | Exception handling | Exponential backoff queue |

---

## Non-Goals

1. **Not replacing human judgment** - System recommends, human decides
2. **Not guaranteeing interviews** - Increases odds, not certainty
3. **Not gaming ethics** - ATS optimization is keyword optimization, not fraud
4. **Not real-time scoring** - Batch processing is acceptable
5. **Not perfect accuracy** - 90% is target, not 100%

---

## Decision Log

| Date | Decision | Rationale | Alternatives Considered |
|------|----------|-----------|------------------------|
| 2026-05-06 | Use Sentence-BERT over USE | Better quality, PyTorch ecosystem | Universal Sentence Encoder (TensorFlow) |
| 2026-05-06 | Ensemble approach | No single algorithm matches LLM | Pure rule-based, pure ML |
| 2026-05-06 | all-MiniLM-L6-v2 model | Best speed/quality tradeoff | all-mpnet-base-v2 (slower) |
| 2026-05-06 | Local-first architecture | Zero cost, privacy, reliability | Cheaper LLM (Haiku) |
| 2026-05-06 | ESCO taxonomy base | Industry standard, 13K skills | Custom taxonomy |
| 2026-05-07 | Hybrid LLM fallback | Handles edge cases local models struggle with | Pure local (lower quality on edges), Pure LLM (defeats cost purpose) |
| 2026-05-07 | en_core_web_sm over lg | 46MB vs 575MB, sufficient for skill extraction | en_core_web_lg (more accurate NER, but 12x memory) |
| 2026-05-07 | msmarco model for asymmetric search | SBERT docs recommend for query-to-document matching | all-MiniLM (symmetric, less optimal) |
| 2026-05-07 | Confidence-weighted level scoring | 40% of jobs have ambiguous levels | Fixed weights (penalizes good matches unfairly) |
| 2026-05-07 | Partial ProfileManager integration | Extract deterministic fields (deal_breakers, remote_requirement, level_goal) for local scoring; pass narrative fields only to LLM fallback | Full integration (personality fields can't be matched locally), No integration (loses valuable user data) |
| 2026-05-07 | CPU-only PyTorch | Saves ~2GB disk, sufficient for inference | GPU version (faster but massive download, most users lack CUDA) |

---

## ProfileManager Integration Strategy

### Decision: Partial Integration

ProfileManager contains two types of data:

**Deterministic (use in v2 local scoring):**
- `deal_breakers` → Fast rejection filter
- `remote_requirement` → Fast rejection filter
- `level_goal` → Input to LevelDetector
- `skills_to_avoid` → Negative scoring signal

**Narrative (LLM fallback only):**
- `work_style`, `pace_preference`, `top_motivations` → Require LLM interpretation
- `superpower`, `unique_background`, `career_arc` → Context for LLM scoring

### Implementation Pattern

```python
class ProfileIntegration:
    """Extract structured data from ProfileManager for v2 scoring."""

    def __init__(self, profile: ProfileManager):
        self.profile = profile

    def get_deal_breakers(self) -> List[str]:
        """Get deterministic deal-breakers for fast filtering."""
        breakers = self.profile.get_deal_breakers()
        if self.profile.get("remote_requirement") == "Full remote (non-negotiable)":
            breakers.append("__REQUIRE_REMOTE__")
        return breakers

    def get_level_goal(self) -> str:
        """Get level goal for LevelDetector."""
        goal = self.profile.get("level_goal", "")
        if "IC" in goal:
            return "senior"  # Map to our level taxonomy
        elif "manager" in goal.lower():
            return "manager"
        return "senior"  # default

    def get_llm_context(self) -> str:
        """Get narrative context for LLM fallback only."""
        return self.profile.get_evaluation_context()
```

---

## Installation Requirements

### Disk Space
- PyTorch (CPU): ~500MB
- sentence-transformers: ~100MB
- spaCy en_core_web_sm: ~46MB
- SkillNER + SKILL_DB: ~50MB
- **Total: ~700MB** (excluding Python environment)

### Memory (Runtime)
- SBERT model (MiniLM): ~90MB
- spaCy model: ~50MB
- Embeddings cache (1k jobs): ~15MB
- **Total typical: ~200MB**

### Installation Commands

```bash
# Install CPU-only PyTorch first (saves ~2GB)
pip install torch --index-url https://download.pytorch.org/whl/cpu

# Install v2 dependencies
pip install sentence-transformers scikit-learn rank-bm25 spacy \
            fuzzywuzzy python-Levenshtein psutil

# Download spaCy model
python -m spacy download en_core_web_sm

# Download NLTK data (one-time)
python -c "import nltk; nltk.download('punkt'); nltk.download('stopwords'); nltk.download('wordnet')"
```

### Verified Compatibility
- Python 3.12: ✓
- Existing dependencies (anthropic, sqlalchemy, requests): ✓ No conflicts
- All v2 dependencies resolve cleanly

---

## References

- Sentence-Transformers Documentation: https://www.sbert.net/
- ESCO Skills Taxonomy: https://esco.ec.europa.eu/
- SkillNER Library: https://github.com/AnasAito/SkillNER
- BM25 Algorithm: Robertson et al., "The Probabilistic Relevance Framework"
- ATS Research: Jobscan.co industry reports

---

## Approval

| Role | Name | Date | Status |
|------|------|------|--------|
| Product Owner | - | - | Pending |
| Technical Lead | - | - | Pending |
| QA Lead | - | - | Pending |

---

**Document Version**: 1.0
**Last Updated**: 2026-05-06
**Next Review**: After v2.0 implementation complete
