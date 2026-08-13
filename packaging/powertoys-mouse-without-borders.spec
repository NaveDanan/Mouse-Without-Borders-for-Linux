# systemd-rpm-macros is not present on every builder (Ubuntu's rpm has no
# such package), so define the udev rules directory when it is missing.
%{!?_udevrulesdir: %global _udevrulesdir %{_prefix}/lib/udev/rules.d}

Name:           powertoys-mouse-without-borders
Version:        @VERSION@
Release:        1%{?dist}
Summary:        Share input and clipboard across Linux and Windows PCs
License:        MIT
URL:            https://github.com/NaveDanan/Mouse-Without-Borders-for-Linux
Source0:        %{name}-%{version}.tar.gz

BuildRequires:  cargo
BuildRequires:  gcc
BuildRequires:  python3 >= 3.10
Requires:       python3 >= 3.10
Requires:       python3-gobject
Requires:       gtk4
Requires:       libadwaita
Requires:       python3-cryptography
Requires:       glibc >= 2.39
Requires:       libgcc
Requires:       xclip
Requires:       wl-clipboard
Requires:       xdg-desktop-portal >= 1.18
Requires:       xdotool
Requires:       polkit
Recommends:     xdg-desktop-portal-gnome

%description
An independent Linux implementation of Mouse Without Borders. It uses the
native encrypted Windows protocol and rootless Wayland desktop portals to share
mouse, keyboard, clipboard input, and file drag-and-drop between Linux and
Windows computers.

%prep
%setup -q

%build
cargo build --manifest-path portal-bridge/Cargo.toml --locked --release
python3 -m compileall -q mwb_linux
python3 packaging/generate-third-party-notices.py portal-bridge/Cargo.toml \
    > THIRD-PARTY-NOTICES.md

%install
mkdir -p \
    %{buildroot}%{_bindir} \
    %{buildroot}%{_prefix}/lib/powertoys-mouse-without-borders \
    %{buildroot}%{_prefix}/lib/systemd/user \
    %{buildroot}%{_datadir}/applications \
    %{buildroot}%{_datadir}/metainfo \
    %{buildroot}%{_datadir}/icons/hicolor/scalable/apps \
    %{buildroot}%{_docdir}/powertoys-mouse-without-borders
cp -a mwb_linux %{buildroot}%{_prefix}/lib/powertoys-mouse-without-borders/
find %{buildroot}%{_prefix}/lib/powertoys-mouse-without-borders \
    -type d -name __pycache__ -prune -exec rm -rf '{}' ';'
install -m 0755 portal-bridge/target/release/mwb-portal-bridge \
    %{buildroot}%{_prefix}/lib/powertoys-mouse-without-borders/mwb-portal-bridge
install -m 0755 resources/powertoys-mouse-without-borders \
    %{buildroot}%{_bindir}/powertoys-mouse-without-borders
install -m 0644 resources/app-io.github.NaveDanan.MouseWithoutBorders.service \
    %{buildroot}%{_prefix}/lib/systemd/user/app-io.github.NaveDanan.MouseWithoutBorders.service
install -m 0644 resources/powertoys-mwb-linux.service \
    %{buildroot}%{_prefix}/lib/systemd/user/powertoys-mwb-linux.service
install -m 0644 resources/io.github.NaveDanan.MouseWithoutBorders.desktop \
    %{buildroot}%{_datadir}/applications/io.github.NaveDanan.MouseWithoutBorders.desktop
install -m 0644 resources/io.github.NaveDanan.MouseWithoutBorders.metainfo.xml \
    %{buildroot}%{_datadir}/metainfo/io.github.NaveDanan.MouseWithoutBorders.metainfo.xml
install -d %{buildroot}%{_udevrulesdir}
install -m 0644 resources/60-powertoys-mouse-without-borders-uinput.rules \
    %{buildroot}%{_udevrulesdir}/60-powertoys-mouse-without-borders-uinput.rules
install -m 0644 mwb_linux/icons/hicolor/scalable/apps/io.github.NaveDanan.MouseWithoutBorders.svg \
    %{buildroot}%{_datadir}/icons/hicolor/scalable/apps/io.github.NaveDanan.MouseWithoutBorders.svg
install -m 0644 README.md \
    %{buildroot}%{_docdir}/powertoys-mouse-without-borders/README.md
install -m 0644 LICENSE \
    %{buildroot}%{_docdir}/powertoys-mouse-without-borders/LICENSE
install -m 0644 THIRD-PARTY-NOTICES.md \
    %{buildroot}%{_docdir}/powertoys-mouse-without-borders/THIRD-PARTY-NOTICES.md

%files
%license %{_docdir}/powertoys-mouse-without-borders/LICENSE
%doc %{_docdir}/powertoys-mouse-without-borders/README.md
%doc %{_docdir}/powertoys-mouse-without-borders/THIRD-PARTY-NOTICES.md
%{_bindir}/powertoys-mouse-without-borders
%{_prefix}/lib/powertoys-mouse-without-borders/
%{_prefix}/lib/systemd/user/app-io.github.NaveDanan.MouseWithoutBorders.service
%{_prefix}/lib/systemd/user/powertoys-mwb-linux.service
%{_datadir}/applications/io.github.NaveDanan.MouseWithoutBorders.desktop
%{_datadir}/metainfo/io.github.NaveDanan.MouseWithoutBorders.metainfo.xml
%{_datadir}/icons/hicolor/scalable/apps/io.github.NaveDanan.MouseWithoutBorders.svg
%{_udevrulesdir}/60-powertoys-mouse-without-borders-uinput.rules

%changelog
* Fri Aug 14 2026 Nave Danan <nave0712@gmail.com> - 0.8.0-1
- Capture screen edges from /dev/input so no permission prompt is needed.

* Fri Aug 14 2026 Nave Danan <nave0712@gmail.com> - 0.7.0-1
- Add opt-in kernel input injection that works on the lock screen.

* Thu Aug 13 2026 Nave Danan <nave0712@gmail.com> - 0.6.0-1
- Keep the connection alive across lid close, suspend, resume and lock screen.

* Thu Aug 13 2026 Nave Danan <nave0712@gmail.com> - 0.5.5-1
- Fix lock-screen wake, reconnect inhibition, and portal grant continuity.

* Thu Aug 13 2026 Nave Danan <nave0712@gmail.com> - 0.5.4-1
- Fix Linux-to-Windows drag capture and add visible drop feedback.

* Wed Aug 12 2026 Nave Danan <nave0712@gmail.com> - 0.5.3-1
- Add encrypted file drag-and-drop, remote dock reveal, and Linux wake continuity.

* Fri Aug 07 2026 Nave Danan <nave0712@gmail.com> - 0.5.2-1
- Add RPM and AppImage releases for x86_64 and aarch64.
- Make top-bar Exit fully and safely shut down every component.
