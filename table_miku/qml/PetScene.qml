import QtQuick
import QtQuick.Particles

Item {
    id: root
    width: 320
    height: 380

    property var spriteMap: ({})
    property string expression: "focus"
    property string previousSource: spriteFor("focus")
    property string currentSource: spriteFor("focus")
    property string keyboardSource: spriteMap.keyboard || ""
    property color accent: "#43d9f5"
    property real transition: 1
    property real popScale: 1
    property real tapPulse: 0
    property real breath: 0
    property real keySweep: 0
    property bool blink: false

    signal petPressed(real x, real y)
    signal petMoved(real x, real y)
    signal petReleased(real x, real y)
    signal petClicked()
    signal petRightClicked(real x, real y)
    signal bubbleClicked()

    property bool bubblePaused: false

    function spriteFor(name) {
        if (name === "smile") {
            return spriteMap.idle || ""
        }
        if (name === "typing" || name === "thinking") {
            return spriteMap.focus || spriteMap.idle || ""
        }
        if (name === "cheer") {
            return spriteMap.happy || spriteMap.idle || ""
        }
        if (name === "alarm") {
            return spriteMap.surprised || spriteMap.idle || ""
        }
        if (name === "rest") {
            return spriteMap.sleepy || spriteMap.idle || ""
        }
        return spriteMap[name] || spriteMap.idle || ""
    }

    function loadSprites(map) {
        spriteMap = map
        keyboardSource = spriteMap.keyboard || ""
        previousSource = spriteFor(expression)
        currentSource = previousSource
    }

    function accentFor(name) {
        if (name === "happy") return "#ff7faf"
        if (name === "cheer") return "#ff7faf"
        if (name === "surprised") return "#ffd166"
        if (name === "alarm") return "#ffd166"
        if (name === "sleepy") return "#8ea0c4"
        if (name === "rest") return "#8ea0c4"
        if (name === "thinking") return "#b79cff"
        if (name === "smile") return "#4fd6c6"
        return "#43d9f5"
    }

    function setExpression(name) {
        var nextSource = spriteFor(name)
        if (nextSource.length === 0) {
            return
        }
        if (name === expression && transition >= 1) {
            return
        }
        previousSource = currentSource
        currentSource = nextSource
        expression = name
        accent = accentFor(name)
        transition = 0
        transitionAnimation.restart()
        popAnimation.restart()
        if (name === "happy" || name === "cheer") {
            sparkleEmitter.burst(22)
        } else if (name === "surprised" || name === "alarm") {
            sparkleEmitter.burst(12)
        } else if (name === "sleepy" || name === "rest") {
            sleepyEmitter.burst(8)
        } else {
            keyEmitter.burst(7)
        }
    }

    function nudge() {
        tapPulse = 1
        tapPulseAnimation.restart()
        sparkleEmitter.burst(20)
    }

    function showBubble(message, seconds) {
        bubblePaused = false
        bubble.border.color = "#8094b8"
        bubble.border.width = 1
        bubbleText.text = message
        bubbleText.maximumLineCount = 8
        var lines = Math.max(1, bubbleText.lineCount)
        bubble.height = Math.min(180, Math.max(72, 42 + lines * 19))
        bubble.y = 4
        bubble.visible = true
        bubbleHideTimer.stop()
        bubbleIn.restart()
        bubbleHideTimer.interval = Math.max(seconds, 3) * 1000
        bubbleHideTimer.start()
    }

    function hideBubble() {
        bubbleOut.restart()
    }

    function pauseBubble() {
        if (!bubblePaused && bubbleHideTimer.running) {
            bubblePaused = true
            bubbleHideTimer.stop()
            bubble.border.color = "#43d9f5"
            bubble.border.width = 2
        }
    }

    function resumeBubble() {
        if (bubblePaused) {
            bubblePaused = false
            bubbleHideTimer.start()
            bubble.border.color = "#8094b8"
            bubble.border.width = 1
        }
    }

    NumberAnimation {
        id: transitionAnimation
        target: root
        property: "transition"
        to: 1
        duration: 260
        easing.type: Easing.OutCubic
    }

    SequentialAnimation {
        id: popAnimation
        PropertyAnimation { target: root; property: "popScale"; to: 1.055; duration: 120; easing.type: Easing.OutCubic }
        PropertyAnimation { target: root; property: "popScale"; to: 1; duration: 260; easing.type: Easing.OutBack }
    }

    NumberAnimation {
        id: tapPulseAnimation
        target: root
        property: "tapPulse"
        to: 0
        duration: 360
        easing.type: Easing.OutCubic
    }

    SequentialAnimation on breath {
        loops: Animation.Infinite
        NumberAnimation { to: 1; duration: 1600; easing.type: Easing.InOutSine }
        NumberAnimation { to: -1; duration: 1700; easing.type: Easing.InOutSine }
    }

    NumberAnimation on keySweep {
        from: 0
        to: 10
        duration: 880
        loops: Animation.Infinite
        easing.type: Easing.Linear
    }

    Timer {
        interval: 3600
        repeat: true
        running: true
        triggeredOnStart: false
        onTriggered: {
            if (root.expression !== "surprised") {
                root.blink = true
                blinkOff.restart()
            }
        }
    }

    Timer {
        id: blinkOff
        interval: 110
        onTriggered: root.blink = false
    }

    Timer {
        id: bubbleHideTimer
        onTriggered: root.hideBubble()
    }

    ParticleSystem {
        id: particles
        anchors.fill: parent
    }

    ItemParticle {
        system: particles
        groups: ["spark"]
        delegate: Rectangle {
            width: 7
            height: 7
            radius: 3.5
            color: root.accent
            opacity: 0.72
        }
    }

    ItemParticle {
        system: particles
        groups: ["sleepy"]
        delegate: Rectangle {
            width: 10
            height: 10
            radius: 5
            color: "#d9e5ff"
            border.width: 1
            border.color: "#89a2ce"
            opacity: 0.58
        }
    }

    Emitter {
        id: keyEmitter
        system: particles
        group: "spark"
        x: 118
        y: 268
        width: 92
        height: 16
        emitRate: root.expression === "typing" ? 28 : (root.expression === "focus" || root.expression === "smile" || root.expression === "thinking" ? 16 : 7)
        lifeSpan: 820
        lifeSpanVariation: 280
        size: 5
        sizeVariation: 3
        velocity: AngleDirection { angle: 270; angleVariation: 50; magnitude: 34; magnitudeVariation: 18 }
    }

    Emitter {
        id: sparkleEmitter
        system: particles
        group: "spark"
        x: 88
        y: 116
        width: 142
        height: 72
        emitRate: 0
        lifeSpan: 980
        lifeSpanVariation: 360
        size: 6
        sizeVariation: 4
        velocity: AngleDirection { angle: 270; angleVariation: 150; magnitude: 74; magnitudeVariation: 42 }
    }

    Emitter {
        id: sleepyEmitter
        system: particles
        group: "sleepy"
        x: 220
        y: 92
        width: 42
        height: 28
        emitRate: root.expression === "sleepy" ? 3 : 0
        lifeSpan: 1500
        lifeSpanVariation: 420
        size: 8
        sizeVariation: 4
        velocity: AngleDirection { angle: 292; angleVariation: 34; magnitude: 36; magnitudeVariation: 16 }
    }

    Rectangle {
        id: aura
        x: 44
        y: 112
        width: 232
        height: 190
        radius: 95
        color: root.accent
        opacity: root.expression === "sleepy" || root.expression === "rest" ? 0.10 : 0.16
        scale: 1 + root.breath * 0.018 + root.tapPulse * 0.05
        layer.enabled: true
        layer.smooth: true
    }

    Rectangle {
        id: shadow
        x: 54
        y: 330
        width: 212 - root.breath * 6
        height: 24
        radius: 12
        color: "#17213a"
        opacity: 0.23
        scale: 1 - root.tapPulse * 0.06
    }

    Item {
        id: petRig
        x: 0
        y: 96 + root.breath * 2.4
        width: 320
        height: 260
        scale: root.popScale + root.tapPulse * 0.04 + (root.expression === "happy" || root.expression === "cheer" ? 0.015 : 0)
        rotation: root.expression === "cheer" ? root.breath * 1.8 : (root.expression === "alarm" ? root.breath * 2.4 : 0)
        transformOrigin: Item.Center

        Image {
            id: previousSprite
            x: 58
            y: root.expression === "surprised" ? 0 : 6
            width: 204
            height: 184
            source: root.previousSource
            fillMode: Image.PreserveAspectFit
            smooth: true
            mipmap: true
            opacity: 1 - root.transition
        }

        Image {
            id: currentSprite
            x: 58
            y: root.expression === "surprised" ? 0 : 6
            width: 204
            height: 184
            source: root.currentSource
            fillMode: Image.PreserveAspectFit
            smooth: true
            mipmap: true
            opacity: root.transition
        }

        Rectangle {
            x: 105
            y: 96
            width: 22
            height: 4
            radius: 2
            color: "#263553"
            opacity: root.blink ? 0.82 : 0
        }

        Rectangle {
            x: 168
            y: 96
            width: 22
            height: 4
            radius: 2
            color: "#263553"
            opacity: root.blink ? 0.82 : 0
        }

        Text {
            x: 224
            y: 38 + root.breath * 4
            text: "Zzz"
            visible: root.expression === "sleepy" || root.expression === "rest"
            color: "#6d80a7"
            font.pixelSize: 18
            font.bold: true
            opacity: 0.78
        }

        Repeater {
            model: 2
            delegate: Rectangle {
                x: 228 + index * 15
                y: 52 + root.breath * 2
                width: 18
                height: 18
                radius: 9
                visible: root.expression === "happy" || root.expression === "cheer"
                color: "#ff8cad"
                opacity: 0.86
            }
        }

        Rectangle {
            x: 236
            y: 65 + root.breath * 2
            width: 24
            height: 24
            rotation: 45
            radius: 4
            visible: root.expression === "happy" || root.expression === "cheer"
            color: "#ff8cad"
            opacity: 0.86
        }

        Rectangle {
            x: 78
            y: 180
            width: 164
            height: 82
            radius: 18
            color: "#f7fbff"
            border.color: "#23304c"
            border.width: keyboard.source === "" ? 3 : 0
            opacity: keyboard.source === "" ? 0.92 : 0
        }

        Image {
            id: keyboard
            x: 24
            y: 172 + root.breath * 0.8
            width: 272
            height: 96
            source: root.keyboardSource
            fillMode: Image.PreserveAspectFit
            smooth: true
            mipmap: true
            opacity: source === "" ? 0 : 0.97
        }

        Repeater {
            model: 2
            delegate: Rectangle {
                x: 70 + (((root.keySweep + index * 4.8) % 10) * 16)
                y: 206 + index * 4 + root.breath * 0.8
                width: 14
                height: 9
                radius: 3
                color: "#82f5ff"
                opacity: 0.62 - index * 0.18
            }
        }
    }

    Rectangle {
        id: bubble
        x: 10
        y: 4
        width: 300
        height: 92
        radius: 22
        color: "#fbfdff"
        border.color: "#8094b8"
        border.width: 1
        opacity: 0
        visible: false

        Text {
            id: bubbleText
            anchors.fill: parent
            anchors.margins: 13
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
            wrapMode: Text.WordWrap
            maximumLineCount: 8
            elide: Text.ElideRight
            minimumPixelSize: 10
            fontSizeMode: Text.Fit
            color: "#263553"
            font.family: "Microsoft YaHei"
            font.pixelSize: 13
            lineHeight: 1.18
        }

        MouseArea {
            anchors.fill: parent
            acceptedButtons: Qt.LeftButton
            onClicked: {
                if (bubblePaused) {
                    resumeBubble()
                } else {
                    pauseBubble()
                }
                root.bubbleClicked()
            }
        }
    }

    NumberAnimation {
        id: bubbleIn
        target: bubble
        property: "opacity"
        to: 0.96
        duration: 180
        easing.type: Easing.OutCubic
    }

    SequentialAnimation {
        id: bubbleOut
        NumberAnimation { target: bubble; property: "opacity"; to: 0; duration: 180; easing.type: Easing.InCubic }
        ScriptAction { script: bubble.visible = false }
    }

    MouseArea {
        anchors.fill: parent
        acceptedButtons: Qt.LeftButton | Qt.RightButton
        hoverEnabled: true
        onPressed: function(mouse) {
            if (mouse.button === Qt.RightButton) {
                root.petRightClicked(mouse.x, mouse.y)
                return
            }
            root.petPressed(mouse.x, mouse.y)
        }
        onPositionChanged: function(mouse) {
            if (pressedButtons & Qt.LeftButton) {
                root.petMoved(mouse.x, mouse.y)
            }
        }
        onReleased: function(mouse) {
            if (mouse.button === Qt.LeftButton) {
                root.petReleased(mouse.x, mouse.y)
                root.petClicked()
            }
        }
    }
}
