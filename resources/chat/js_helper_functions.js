function getElementById(id) {
    return document.getElementById(id);
}

function getElement(query) {
    return document.querySelector(query);
}

function removeElement(query) {
    let elem = getElement(query);
    if (elem) {
        elem.remove();
    }
}

function parseHtml(html) {
    let template = document.createElement('template');
    template.innerHTML = html;
    return template.content;
}

function replaceElement(query, content) {
    let element = getElement(query);
    if (element) {
        content = parseHtml(content);
        element.replaceWith(content);
    }
}

function appendElement(query, content) {
    let element = getElement(query);
    if (element) {
        content = parseHtml(content);
        element.append(content);
    }
}

function emptyElement(query) {
    let elem = getElement(query);
    if (elem) {
        while (elem.firstChild) {
            elem.firstChild.remove();
        }
    }
}

function previousSibling(id, content) {
    let elem = getElementById(id);
    if (elem) {
        content = parseHtml(content);
        elem.previousElementSibling.append(content);
    }
}

function prependOutside(id, content) {
    let elem = getElementById(id);
    if (elem) {
        content = parseHtml(content);
        elem.before(content);
    }
}

function updateElement(query, content) {
    let elem = getElement(query);
    if (elem) {
        while (elem.firstChild) {
            elem.firstChild.remove();
        }
        content = parseHtml(content);
        elem.append(content);
    }
}

function addContextMenuToElement(query) {
    let elem = getElement(query);
    if (elem) {
        elem.addEventListener('contextmenu', handleContextMenu);
    }
}
function appendMessageToChat(content) {
    removeElement('#insert');
    appendElement('#chat', content);
}

function styleElement(query, property, value) {
    let elem = getElement(query);
    if (elem) {
        elem.style.setProperty(property, value);
    }
}

function getHeightElement(query) {
    let elem = getElement(query);
    if (elem) {
        return elem.offsetHeight;
    }
    return 0;
}

function scrollToBottom() {
    setTimeout(window.scrollTo(0, document.body.scrollHeight), 5);
}

function print(content) {
    console.error(content);
}

function handleContextMenu(e) {
    let id = e.target.getAttribute('id');
    if (id === null) {
        id = e.target.offsetParent.getAttribute('id')
    }
    if (id === null) {
        id = e.target.offsetParent.offsetParent.getAttribute('id')
    }
    chat.handleContextMenuEvent(id);
}

let timer = null;
let tries = 3;
function startWebChannel() {
    if (typeof QWebChannel === undefined) {
        tries = tries - 1;
        if (tries > 0) {
            if (timer) {
                clearTimeout(timer);
            }
            timer = setTimeout(startWebChannel, 1000);
        }
        return
    }
    new QWebChannel(qt.webChannelTransport, function(channel) {
        window.chat = channel.objects.chat
    })
}
