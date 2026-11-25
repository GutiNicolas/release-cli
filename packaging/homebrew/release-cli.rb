# Source of Formula/release-cli.rb in the GutiNicolas/homebrew-tap repo.
# Per release: set url to the new tag and sha256 to the hash of that tarball
# (steps in docs/features/brew-install/ROADMAP.md, "Release steps").
class ReleaseCli < Formula
  include Language::Python::Virtualenv

  desc "Cut release candidates and final versions for Maven, Gradle, sbt and Go projects"
  homepage "https://github.com/GutiNicolas/release-cli"
  url "https://github.com/GutiNicolas/release-cli/archive/refs/tags/v0.4.0.tar.gz"
  sha256 "SHA256_OF_THE_TAG_TARBALL"
  license "GPL-3.0-only"
  head "https://github.com/GutiNicolas/release-cli.git", branch: "main"

  depends_on "python@3.15"

  def install
    virtualenv_install_with_resources
  end

  test do
    assert_match version.to_s, shell_output("#{bin}/release --version")
  end
end
