Icon and backdrop from [grove](https://github.com/Garfield1002/grove) (MIT, same author):
the tree of `packaging/grove.svg`, printed in ink (`#24231f`) on paper (`#f7f6f3`) for the `icon-*.png` sizes and
`logo.png` (the page itself draws the tree as a CSS mask in `--fg`), and `crates/grove/assets/backdrop.svg` as is.
`badge-96.png` is the notification badge. The icons, badge and backdrop are served by the daemon from `/assets/`
without the login token (they hold nothing private); `logo.png` is only for README.md and isn't served.
