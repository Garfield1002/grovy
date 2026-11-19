/* grovy custom script: copy to ~/.config/grovy/custom.js (grovy setup does this) and edit.
   It runs in the grovy page after grovy's own script and its built-in extensions (extensions/ in the grovy folder,
   such as the context bar: read them for longer examples). It is picked up on the next page reload (no daemon
   restart), and it's kept outside the repo, so `git pull` never conflicts with it. Everything below is commented out.

   Use only the `grovy` object; the rest of the page's code may change between versions.

     grovy.h(tag, attrs, ...children)   build DOM safely (text is never parsed as HTML)
     grovy.md(text)                     markdown -> DOM, the way replies are drawn
     grovy.api(path, body?)             call grovy's API (GET, or POST with a JSON body), plugins' routes included
     grovy.ask(msg, ok) / grovy.say(msg)    a yes/no dialog (-> Promise<boolean>) / a message
     grovy.sheet(...nodes), grovy.closeSheet()   open your own dialog
     grovy.css(text)                    add styles of your own
     grovy.slot("composer")             an element above the message box (below the reply buttons) to add things to
     grovy.session, grovy.state         the open session's name (null on the home screen) / a copy of its state
     grovy.draft(text)                  put text in the message box for you to send
     grovy.send(text)                   send now, like the Send button (send hooks apply; false if one stopped it)

     grovy.widget(type, render)         a ```widget type Claude can draw: render(spec) returns nodes, and throws an
                                        Error with a readable message for bad input. Tell Claude about it in
                                        ~/.config/grovy/instructions.md (added to every session's instructions).
     grovy.menu(label, run, {where})    an item in the ⋯ menu: where "session" (default), "home" or "both"; add
                                        when: state => true/false to show it only sometimes (state: the open
                                        session's, null on the home screen); add section: "Conversation" | "Session"
                                        | your own name to group it under a header in the session menu
     grovy.key(combo, run)              a shortcut such as "Ctrl+K", "Alt+Shift+N", "Ctrl+?" or "F2" (Cmd counts as
                                        Ctrl). It needs Ctrl or Alt (F-keys excepted) and can't be one of grovy's own.
     grovy.on("render", (node, event))  a message was drawn: add to it
     grovy.on("update", state)          the open session changed (a copy of its state)
     grovy.on("route", session)         another session (or the home screen) was opened
     grovy.on("send", text)             return new text to send instead, or false to keep it from being sent
                                        (answer at once: an async hook stops the send)
     grovy.extension(name, setup)       a named extension: setup(grovy) runs at start-up
     grovy.disable(name)                turn off a built-in extension, e.g. grovy.disable("context-bar")
     grovy.extensions                   the named extensions, and whether each is on

   Claude's words and tool inputs are untrusted: build with grovy.h / grovy.md, never innerHTML.
   An error in here shows up at the top of the page until you tap it; grovy itself keeps working. */


/* No context bar above the message box:

grovy.disable("context-bar");
*/


/* A ⋯ menu item that asks Claude to keep going:

grovy.menu("Continue", () => grovy.send("continue"), {sub: "Send “continue”"});
*/


/* Alt+R: put a standard request in the message box (you still review and send it):

grovy.key("Alt+R", () => grovy.draft("Review your last change for bugs before we go on."));
*/


/* A "Read aloud" button under each of Claude's replies:

grovy.on("render", (node, e) => {
  if (e.type !== "Stop" || !e.data.last_assistant_message) return;
  const text = e.data.last_assistant_message;
  node.append(grovy.h("button", {class: "chip", onclick: () => speechSynthesis.speak(new SpeechSynthesisUtterance(text))},
    "Read aloud"));
});
*/


/* Never send a message that looks like it contains an API key:

grovy.on("send", text => /\bsk-[A-Za-z0-9_-]{20,}/.test(text) ? (grovy.say("That looks like a secret: not sent."), false) : text);
*/


/* A widget type of your own. In ~/.config/grovy/instructions.md, tell Claude:
     For short task lists, use a ```widget block: {"type":"todo","items":["first task","second task"]}

grovy.widget("todo", spec => {
  if (!Array.isArray(spec.items) || !spec.items.length) throw new Error("items must be a non-empty list");
  return grovy.h("ul", {}, ...spec.items.slice(0, 50).map(t => grovy.h("li", {}, String(t))));
});
*/
