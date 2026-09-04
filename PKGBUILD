# Maintainer: Isaac Priestley <progressions@gmail.com>
pkgname=clip-editor
pkgver=0.9.1
pkgrel=1
pkgdesc="Native GTK clip editor for social video exports"
arch=('any')
url="https://github.com/progressions/clip-editor"
license=('LicenseRef-Proprietary')
depends=(
  'ffmpeg'
  'gdk-pixbuf2'
  'gst-libav'
  'gst-plugins-base'
  'gst-plugins-good'
  'gtk4'
  'libadwaita'
  'python'
  'python-gobject'
)
makedepends=('git' 'python-build' 'python-installer' 'python-setuptools' 'python-wheel')
optdepends=('chromium: legacy clip-editor serve --open interface')
source=("$pkgname::git+$url.git#tag=v$pkgver")
sha256sums=('SKIP')

build() {
  cd "$pkgname"
  # setuptools stages into build/lib and does not prune files that went away, so
  # a rebuild over an existing extract (makepkg -e, --noextract) can carry a
  # removed module into the wheel. This PR also gitignores build/, so the
  # directory now sticks around in working trees.
  rm -rf build dist
  python -m build --wheel --no-isolation
}

check() {
  cd "$pkgname"
  python -m unittest discover -s tests
  python -m clip_editor selftest
}

package() {
  cd "$pkgname"
  python -m installer --destdir="$pkgdir" dist/*.whl
  install -Dm644 clip-editor.desktop \
    "$pkgdir/usr/share/applications/clip-editor.desktop"
}
