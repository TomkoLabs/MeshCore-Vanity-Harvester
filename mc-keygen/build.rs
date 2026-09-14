fn main() {
    println!("cargo:rerun-if-changed=cuda/harvest_filter.h");
    println!("cargo:rerun-if-changed=src/harvest_filter.c");
    cc::Build::new().file("src/harvest_filter.c").compile("harvest_filter");
}
