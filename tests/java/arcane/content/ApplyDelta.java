package arcane.content;

import java.nio.file.Files;
import java.nio.file.Paths;

/** Test harness: java arcane.content.ApplyDelta ORIGINAL DELTA OUT (prints "error: ..." when the delta is refused). */
public final class ApplyDelta {
    public static void main(String[] args) throws Exception {
        try {
            byte[] out = GamedataDelta.apply(Files.readAllBytes(Paths.get(args[0])), Files.readAllBytes(Paths.get(args[1])));
            Files.write(Paths.get(args[2]), out);
            System.out.println("ok");
        } catch (IllegalArgumentException e) {
            System.out.println("error: " + e.getMessage());
        }
    }
}
