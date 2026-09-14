# Longer patterns, search odds and hardware

The useful objective is to test more distinct keys against more acceptable
patterns, and retain the longest worthwhile results. Changing private keys
does not let us steer the next public-key digit toward a desired value.
The native backend already amortizes expensive curve operations with a `+8B`
point chain and Montgomery batch inversion; these are substantial algorithmic
improvements over deriving every candidate from a new random seed.

## What changed

- **First six characters first:** a visible-ID gate requires a catalog/curated
  prefix, four leading equal digits, five ascending/descending digits,
  `ABABAB`, `ABCABC`, `AAABBB`, `AABBCC`, or a six-character palindrome. A word
  longer than six may qualify by its visible part (`DEADBE` for `DEADBEEF`).
- Every scored pattern begins at character zero. Runs, sequences, periods,
  palindromes, words, repeated/extended words and arbitrary word chains can
  extend beyond the ID. There is no 16-character span ceiling. A long repeated
  unit whose first six characters look random does not qualify.
- `DEADBEEFFFFF…`, `123456789ABC…`, and `222222222222…` qualify.
  `A73C91…222222222222` does not. Adding a disconnected tail to an otherwise
  good ID also adds no score. Visible-ID eligibility comes before rarity;
  among eligible keys, rarity ranks the continuous leading patterns.
- Native screening stops at prefix mismatches, rather than scanning every
  position. The GPU checks three bytes before computing the x-coordinate sign,
  avoiding that multiplication for most keys. The complete sign bit is still
  checked for patterns reaching the final byte.
- Thresholds are generated from the Python scorer and current cutoff, with a
  default native score floor of 28 million. A shared trie handles all catalog
  words. Python ranks and independently verifies every accepted pair.
- Results stream continuously. Device buffers persist between launches and
  exact attempt statistics arrive even without a match. Overflow replays the
  same independent starts in smaller groups; replay work is counted separately.
- Each short chain starts independently from OS randomness and retires on its
  first hit. Saving several nearby scalar values from one chain would produce
  related private keys; this implementation avoids that relationship.
- GPU startup performs a mandatory CPU/device comparison of multiple threads,
  chain advancement, sign bits, counters and forced overflow. Graceful shutdown
  drains already-found results before the final checkpoint.
- Python uses the same visible-ID gate, a first-three-character word index,
  and anchored measurements. Native and Python filters are tested against
  authoritative full scoring, including score boundaries and 64-character spans.
- Compound words followed by a repeated final character can earn credit for
  the whole span, such as `C0FFEEBEEFFFFF`. Intermediate word-chain endpoints
  are retained so that one interpretation cannot hide a better extension.
  Extension rarity charges for both catalog combinations and the choice of
  where the word chain ends. The extra boundary charge is conservative; the
  new rare tail patterns have not been independently empirically calibrated.
  `DEADBEEFFFFF` already worked because `DEADBEEF` is itself a catalog word.
- In optional legacy `--prefix-campaigns` mode, targets include all ascending and descending starts, compound-word tails
  from the strongest word pairs, and lengths through 16 rather than 14.
  This remains a finite catalog: it does not enumerate all possible word chains.
- Campaign rotation tracks each prefix's attempts across regrouping and
  restarts. Among equally attempted campaigns that fit the time budget, longer
  targets take precedence. Shorter untried campaigns still receive turns.
  The score estimate is a scheduling heuristic, not an upper bound on the
  quality of a lucky suffix.
- Sequences have their own leaderboard, available through
  `./run.sh --top 20 --board sequence`. Saved keys are rescored on the next
  startup under scoring revision 11. Existing snapshots remain readable. Before rescoring,
  old private snapshots are preserved as `before_score_11_*.private.json` with
  mode `0600`. Entries earning zero under the new rules leave the active boards;
  their key material remains in that archive. Empty boards retain work totals.

After visible-ID eligibility, estimated rarity dominates aesthetic bonuses. Within a run or a fixed word
extension, an additional constrained hex character earns four bits. Raw visual
length alone is insufficient across families: a 12-character palindrome only
constrains six characters, while a 12-character repeated digit constrains 11.

## How much harder is the next character?

Assume approximately uniform public-key prefix nibbles. For `M` distinct,
equally long exact prefixes of length `L` that are all tested on every key:

```text
probability per attempt p = M / 16^L
expected attempts        = 16^L / M
mean waiting time        = 16^L / (M × keys_per_second)
chance by time t         ≈ 1 − exp(−keys_per_second × t × p)
median waiting time      ≈ 0.693 × mean
95% chance time          ≈ 2.996 × mean
```

The exponential formulas approximate independent trials at rare probabilities.
They are estimates, not deadlines. At the mean waiting time there is still
about a 37% chance of having found nothing, and prior misses do not make the
next attempt more likely to succeed.

For example, accepting any of the **14 allowed leading repeated digits**
(`1`–`9`, `A`–`E`) at an illustrative **500 million keys/second** gives:

| Leading run length | Mean time | Approximate time for 95% chance |
|---|---:|---:|
| 10 | 2.6 minutes | 7.8 minutes |
| 11 | 42 minutes | 2.1 hours |
| 12 | 11.2 hours | 33.5 hours |
| 13 | 7.4 days | 22.3 days |
| 14 | 119 days | 357 days |

This is an illustration, **not a benchmark of either Kraken or GX10**. Multiply
the times by `500 million / measured keys_per_second` for another sustained
rate. Hunting only `222…` rather than any allowed digit takes 14 times longer.
If runs receive only one quarter of search time, multiply wall-clock times by
four as well. Those restrictions apply to legacy exact-prefix campaigns.
Prefix harvesting checks every configured family on each tested key and keeps
searching a family after a find.

Scanning elsewhere in the key would increase the number of accepted results,
but those extra positions do not improve the visible repeater ID. This version
spends its screening work on leading patterns. Broad coverage here means more
acceptable **prefix families**, not more start positions. Each native candidate
is considered against all configured prefix families, with no campaign rotation.
This increases opportunities relative to a small active prefix list; it does
not bias the cryptographic output or guarantee a throughput improvement.

There is no special cryptographic wall at 12 or 14 characters. For a fixed
number of acceptable targets, one extra character needs about 16 times the
work, and two extra characters need about 256 times. A typical attainable
length grows approximately as `log16(M × rate × time)`. A 2× speedup buys only
one quarter of a character at the same search duration; a 16× speedup buys
about one character. Ordered, nonwrapping hex sequences have their own natural
maximum of 16 digits. Other spans can reach all 64 characters in prefix
harvesting. The optional legacy catalog retains a default ceiling of 16,
configurable to 62. Neither mode removes the exponential work needed for
tightly constrained patterns.

## Kraken, GX10 and other hardware

Compare sustained keys/second using the same score floor:

```bash
.venv/bin/python scripts/benchmark_harvest.py --seconds 60
.venv/bin/python scripts/benchmark_harvest.py --cpu --threads 8 --seconds 60
```

Benchmark mode prints statistics only, never private keys. Divide the final
`attempts` by `elapsed_secs` for the useful rate. The new path counts only
candidates actually tested, and separates overflow replay. Legacy short-prefix
GPU search can overcount partial launches and is not a valid performance
baseline for current harvesting. Python scoring and GPU generation are different
stages; improving one does not multiply the other's throughput.

Local comparison on one pinned Ryzen 7 5800X core, at the same 28-bit floor
and with two alternating five-second native runs per version:

| Native CPU search | Mean sustained rate |
|---|---:|
| Previous all-position filter | 0.588 million keys/s |
| Visible-ID and anchored-prefix filter | 1.799 million keys/s |

That is about **3.06×** for the native CPU search in this short local test.
The isolated C screening loop was about 133× faster on the same 100,000 random
32-byte inputs, but curve arithmetic dominates the rest of the search, so
that filter-only number is not an end-to-end speedup. These are CPU results,
not RTX 3080 or GX10 predictions. A longer benchmark on each deployment host
should determine its sustained rate.

The [ASUS GX10](https://www.asus.com/us/networking-iot-servers/desktop-ai-supercomputer/ultra-small-ai-supercomputers/asus-ascent-gx10/)
uses a GB10 Blackwell GPU and advertises FP4/Tensor Core AI performance. This
key generator uses integer field arithmetic. Its source does not use Tensor
Cores, so advertised AI throughput and unified-memory capacity do not predict
whether it outperforms an RTX 3080 for this workload.

CUDA compilation selects the detected compute capability, so the installed
NVRTC must support that architecture. This source compiled with CUDA 12.9 NVRTC
for compute 8.6 and 12.1 during validation. The locked Rust CUDA dependency
requires Rust 1.88+. The existing integer field arithmetic is unchanged. Only
candidate screening and when the sign multiplication is needed have changed.

Hardware tuning still requires measurements. Register spills, batch-inversion
size, block size and resident blocks trade arithmetic cost against memory
pressure. NVIDIA's [CUDA Best Practices Guide](https://docs.nvidia.com/cuda/cuda-c-best-practices-guide/index.html#occupancy)
explains why maximum occupancy does not necessarily maximize performance.

Validation here includes Rust CPU/CUDA builds, native CPU streaming,
differential filter tests against Python, host execution of the harvest kernel,
and NVRTC compilation. **No GPU was available for device execution or a
Kraken/GX10 throughput measurement.** The mandatory runtime self-test must pass
on the actual GPU before harvesting begins. Compilation and host tests do not
substitute for device execution.

## Ranking accuracy

Scores use an approximate family rarity model with capped aesthetic bonuses.
They are useful for comparing leading patterns, not exact probabilities for the
combined leaderboard. The family corrections inherited from the previous
all-position model are conservative starting values after narrowing eligibility.
A 400,000-key check of the new model measured about +0.23 bits for short runs,
−1.74 for words and −1.67 for palindromes at the deepest thresholds supported by
at least 40 observations. Other families were too rare in that sample to assess.
Long word-chain tails also lack independent empirical calibration. Do not infer
precise month-scale waiting times from a leaderboard score; use explicit target
probabilities and your measured sustained rate instead.
