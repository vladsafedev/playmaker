# Template Homebrew formula for the playmaker CLI.
#
# This file lives in this repo for reference; the live copy goes into the tap
# repo at github.com/shulyugin/homebrew-playmaker (path Formula/playmaker.rb).
#
# After publishing a new PyPI release:
#   1. bump `url` and `sha256` to the new sdist on PyPI:
#        url "https://files.pythonhosted.org/packages/source/p/playmaker-cli/playmaker_cli-X.Y.Z.tar.gz"
#        sha256 "$(curl -sSL <url> | shasum -a 256 | cut -d' ' -f1)"
#   2. regenerate Python deps with `brew update-python-resources playmaker`
#   3. commit to the tap repo, tag if you want
#
# Users install via:
#   brew tap shulyugin/playmaker
#   brew install playmaker

class Playmaker < Formula
  include Language::Python::Virtualenv

  desc "Playing-coach CLI for orchestrating Claude/Codex/Gemini sub-agents in parallel"
  homepage "https://github.com/shulyugin/playmaker"
  url "https://files.pythonhosted.org/packages/source/p/playmaker-cli/playmaker_cli-0.1.0.tar.gz"
  sha256 "REPLACE_WITH_RELEASE_SHA256"
  license "MIT"

  depends_on "python@3.13"

  # Generated with `brew update-python-resources playmaker` after first release.
  # Keep typer + rich + their transitive deps here.
  resource "click" do
    url "https://files.pythonhosted.org/packages/source/c/click/click-REPLACE.tar.gz"
    sha256 "REPLACE"
  end

  resource "markdown-it-py" do
    url "https://files.pythonhosted.org/packages/source/m/markdown-it-py/markdown_it_py-REPLACE.tar.gz"
    sha256 "REPLACE"
  end

  resource "mdurl" do
    url "https://files.pythonhosted.org/packages/source/m/mdurl/mdurl-REPLACE.tar.gz"
    sha256 "REPLACE"
  end

  resource "pygments" do
    url "https://files.pythonhosted.org/packages/source/p/pygments/pygments-REPLACE.tar.gz"
    sha256 "REPLACE"
  end

  resource "rich" do
    url "https://files.pythonhosted.org/packages/source/r/rich/rich-REPLACE.tar.gz"
    sha256 "REPLACE"
  end

  resource "shellingham" do
    url "https://files.pythonhosted.org/packages/source/s/shellingham/shellingham-REPLACE.tar.gz"
    sha256 "REPLACE"
  end

  resource "typer" do
    url "https://files.pythonhosted.org/packages/source/t/typer/typer-REPLACE.tar.gz"
    sha256 "REPLACE"
  end

  resource "typing-extensions" do
    url "https://files.pythonhosted.org/packages/source/t/typing-extensions/typing_extensions-REPLACE.tar.gz"
    sha256 "REPLACE"
  end

  def install
    virtualenv_install_with_resources
  end

  test do
    assert_match "playmaker", shell_output("#{bin}/playmaker --help")
  end
end
