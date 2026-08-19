use criterion::{criterion_group, criterion_main, Criterion};
use mc_keygen::keygen::generate_keypair;
use mc_keygen::search::PrefixMatcher;

/// Benchmark the full keygen + prefix-check pipeline per key.
/// This measures the overhead of multi-prefix matching relative to keygen cost.
fn bench_keygen_pipeline(c: &mut Criterion) {
    let mut group = c.benchmark_group("keygen_pipeline");
    group.throughput(criterion::Throughput::Elements(1));

    let seed = [42u8; 32];

    // Baseline: keygen only (no prefix check)
    group.bench_function("keygen_only", |b| {
        b.iter(|| {
            let kp = generate_keypair(&seed);
            criterion::black_box(&kp);
        })
    });

    // Keygen + 1 prefix check (miss — worst case for single prefix)
    let matcher_1 = PrefixMatcher::new("FF");
    group.bench_function("keygen_1prefix", |b| {
        b.iter(|| {
            let kp = generate_keypair(&seed);
            criterion::black_box(matcher_1.matches(&kp.public_key));
        })
    });

    // Keygen + 5 prefix checks (all miss)
    let matchers_5: Vec<PrefixMatcher> = ["F1", "F2", "F3", "F4", "F5"]
        .iter()
        .map(|p| PrefixMatcher::new(p))
        .collect();
    group.bench_function("keygen_5prefixes", |b| {
        b.iter(|| {
            let kp = generate_keypair(&seed);
            criterion::black_box(matchers_5.iter().any(|m| m.matches(&kp.public_key)));
        })
    });

    // Keygen + 10 prefix checks (all miss)
    let matchers_10: Vec<PrefixMatcher> = ["F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9", "FA"]
        .iter()
        .map(|p| PrefixMatcher::new(p))
        .collect();
    group.bench_function("keygen_10prefixes", |b| {
        b.iter(|| {
            let kp = generate_keypair(&seed);
            criterion::black_box(matchers_10.iter().any(|m| m.matches(&kp.public_key)));
        })
    });

    // Keygen + 20 prefix checks (all miss)
    let matchers_20: Vec<PrefixMatcher> = (0x01u8..=0x14)
        .map(|i| PrefixMatcher::new(&format!("{:02X}", i)))
        .collect();
    group.bench_function("keygen_20prefixes", |b| {
        b.iter(|| {
            let kp = generate_keypair(&seed);
            criterion::black_box(matchers_20.iter().any(|m| m.matches(&kp.public_key)));
        })
    });

    group.finish();
}

criterion_group!(benches, bench_keygen_pipeline);
criterion_main!(benches);
