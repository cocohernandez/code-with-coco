// Grabs one frame from a video at an exact timestamp.
//
// QuickLook only ever hands back the poster frame, which is whatever the video
// happens to open on -- often a blink, a black frame, or a half-formed word.
// AVFoundation can seek anywhere, so this is what lets you choose the cover.
//
//     swift framegrab.swift reel2.MOV 4.5 cover.png
//
// Needs no install: AVFoundation ships with macOS and swift comes with the
// Xcode command line tools. dashboard.py falls back to QuickLook without it.

import AVFoundation
import AppKit
import Foundation

let args = CommandLine.arguments
guard args.count >= 4, let seconds = Double(args[2]) else {
    FileHandle.standardError.write(
        Data("usage: framegrab <video> <seconds> <out.png>\n".utf8))
    exit(2)
}

let asset = AVURLAsset(url: URL(fileURLWithPath: args[1]))
let generator = AVAssetImageGenerator(asset: asset)
// Respect how the phone was held, or portrait clips come out sideways.
generator.appliesPreferredTrackTransform = true
// Land on the frame asked for rather than the nearest keyframe.
generator.requestedTimeToleranceBefore = .zero
generator.requestedTimeToleranceAfter = .zero

do {
    let image = try generator.copyCGImage(
        at: CMTime(seconds: seconds, preferredTimescale: 600), actualTime: nil)
    guard let png = NSBitmapImageRep(cgImage: image)
        .representation(using: .png, properties: [:]) else { exit(3) }
    try png.write(to: URL(fileURLWithPath: args[3]))
} catch {
    FileHandle.standardError.write(Data("\(error)\n".utf8))
    exit(1)
}
