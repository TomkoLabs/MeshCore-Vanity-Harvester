use criterion::{black_box, criterion_group, criterion_main, Criterion};
use mc_keygen::search::PrefixMatcher;

fn make_key(prefix_byte: u8) -> [u8; 32] {
    let mut key = [0x11u8; 32];
    key[0] = prefix_byte;
    key
}

fn bench_prefix_match(c: &mut Criterion) {
    let mut group = c.benchmark_group("prefix_match");

    // Single prefix (old behavior)
    let matcher_1 = PrefixMatcher::new("AB");
    let key_hit = make_key(0xAB);
    let key_miss = make_key(0xCD);

    group.bench_function("1_prefix_hit", |b| {
        b.iter(|| matcher_1.matches(black_box(&key_hit)))
    });
    group.bench_function("1_prefix_miss", |b| {
        b.iter(|| matcher_1.matches(black_box(&key_miss)))
    });

    // Multi-prefix: check any() over N matchers (simulates new code path)
    let prefixes_5: Vec<(String, PrefixMatcher)> = ["AB", "CD", "EF", "12", "34"]
        .iter()
        .map(|p| (p.to_string(), PrefixMatcher::new(p)))
        .collect();

    group.bench_function("5_prefixes_hit_last", |b| {
        let key = make_key(0x34); // matches last prefix
        b.iter(|| {
            prefixes_5
                .iter()
                .any(|(_, m)| m.matches(black_box(&key)))
        })
    });
    group.bench_function("5_prefixes_miss", |b| {
        let key = make_key(0x99);
        b.iter(|| {
            prefixes_5
                .iter()
                .any(|(_, m)| m.matches(black_box(&key)))
        })
    });

    let prefixes_10: Vec<(String, PrefixMatcher)> =
        ["AB", "CD", "EF", "12", "34", "56", "78", "9A", "BC", "DE"]
            .iter()
            .map(|p| (p.to_string(), PrefixMatcher::new(p)))
            .collect();

    group.bench_function("10_prefixes_hit_last", |b| {
        let key = make_key(0xDE);
        b.iter(|| {
            prefixes_10
                .iter()
                .any(|(_, m)| m.matches(black_box(&key)))
        })
    });
    group.bench_function("10_prefixes_miss", |b| {
        let key = make_key(0x99);
        b.iter(|| {
            prefixes_10
                .iter()
                .any(|(_, m)| m.matches(black_box(&key)))
        })
    });

    // Longer prefix (4 hex chars = 2 bytes)
    let matcher_long = PrefixMatcher::new("ABCD");
    let mut key_long_hit = [0x11u8; 32];
    key_long_hit[0] = 0xAB;
    key_long_hit[1] = 0xCD;

    group.bench_function("1_prefix_4char_hit", |b| {
        b.iter(|| matcher_long.matches(black_box(&key_long_hit)))
    });
    group.bench_function("1_prefix_4char_miss", |b| {
        b.iter(|| matcher_long.matches(black_box(&key_miss)))
    });

    group.finish();
}

criterion_group!(benches, bench_prefix_match);
criterion_main!(benches);
