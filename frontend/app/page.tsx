import VoiceAgent from "@/components/VoiceAgent";
import { VoiceProvider } from "@/components/VoiceProvider";

export default function Home() {
  return (
    <VoiceProvider>
      <VoiceAgent />
    </VoiceProvider>
  );
}
