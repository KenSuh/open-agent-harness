// oah-desktop 진입점: 모든 일은 라이브러리의 run 이 맡는다.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    oah_desktop_lib::run()
}
