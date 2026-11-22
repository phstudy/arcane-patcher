package arcane.multiplayer;

import java.awt.image.BufferedImage;
import java.io.File;

import javax.imageio.ImageIO;

/** Test helper (desktop JVM only): decodes PNG files with the multiplayer app's QR decoder, then reads the server address
 *  from the code as the scanner does. One line per file: the address, "error: ..." or "no code". */
public final class DecodePng {
    public static void main(String[] args) throws Exception {
        QrDecoder decoder = new QrDecoder();
        for (String path : args) {
            BufferedImage img = ImageIO.read(new File(path));
            int w = img.getWidth(), h = img.getHeight();
            String text = decoder.decodeArgb(img.getRGB(0, 0, w, h, null, 0, w), w, h);
            if (text == null) {
                System.out.println("no code");
                continue;
            }
            try {
                System.out.println(JoinLink.parseScanned(text));
            } catch (IllegalArgumentException e) {
                System.out.println("error: " + e.getMessage());
            }
        }
    }
}
