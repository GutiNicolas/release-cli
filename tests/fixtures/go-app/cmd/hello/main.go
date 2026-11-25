package main

import (
	_ "embed"
	"fmt"
	"os"
)

// version is stamped at build time: -ldflags "-X main.version=..."
var version = "dev"

//go:embed greeting.txt
var greeting string

func main() {
	if len(os.Args) > 1 && os.Args[1] == "version" {
		fmt.Println(version)
		return
	}
	fmt.Print(greeting)
}
