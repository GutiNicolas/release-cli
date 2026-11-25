fn main() {
    println!("hello {} ({})", env!("CARGO_PKG_VERSION"), greet::greeting("world"));
}
