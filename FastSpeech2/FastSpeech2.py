import os
from abc import ABC

import torch
import torch.nn.functional as F

from FastSpeech2.FastSpeech2Loss import FastSpeech2Loss
from Layers.Conformer import Conformer
from Layers.DurationPredictor import DurationPredictor
from Layers.LengthRegulator import LengthRegulator
from Layers.PostNet import PostNet
from Layers.VariancePredictor import VariancePredictor
from Utility.utils import initialize
from Utility.utils import make_non_pad_mask
from Utility.utils import make_pad_mask


class FastSpeech2(torch.nn.Module, ABC):
    """
    FastSpeech2 module.

    This is a module of FastSpeech2 described in `FastSpeech 2: Fast and
    High-Quality End-to-End Text to Speech`_. Instead of quantized pitch and
    energy, we use token-averaged value introduced in `FastPitch: Parallel
    Text-to-speech with Pitch Prediction`_. The encoder and decoder are Conformers
    instead of regular Transformers.

        https://arxiv.org/abs/2006.04558
        https://arxiv.org/abs/2006.06873
        https://arxiv.org/pdf/2005.08100

    """

    def __init__(self,  # network structure related
                 idim: int, odim: int, adim: int = 384, aheads: int = 4, elayers: int = 6, eunits: int = 1536, dlayers: int = 6, dunits: int = 1536,
                 postnet_layers: int = 5, postnet_chans: int = 256, postnet_filts: int = 5, positionwise_layer_type: str = "conv1d",
                 positionwise_conv_kernel_size: int = 1, use_scaled_pos_enc: bool = True, use_batch_norm: bool = True, encoder_normalize_before: bool = True,
                 decoder_normalize_before: bool = True, encoder_concat_after: bool = False, decoder_concat_after: bool = False, reduction_factor=1,
                 # encoder / decoder
                 conformer_pos_enc_layer_type: str = "rel_pos", conformer_self_attn_layer_type: str = "rel_selfattn", conformer_activation_type: str = "swish",
                 use_macaron_style_in_conformer: bool = True, use_cnn_in_conformer: bool = True, conformer_enc_kernel_size: int = 7,
                 conformer_dec_kernel_size: int = 31,  # duration predictor
                 duration_predictor_layers: int = 2, duration_predictor_chans: int = 256, duration_predictor_kernel_size: int = 3,  # energy predictor
                 energy_predictor_layers: int = 2, energy_predictor_chans: int = 256, energy_predictor_kernel_size: int = 3,
                 energy_predictor_dropout: float = 0.5, energy_embed_kernel_size: int = 1, energy_embed_dropout: float = 0.0,
                 stop_gradient_from_energy_predictor: bool = True,  # pitch predictor
                 pitch_predictor_layers: int = 5, pitch_predictor_chans: int = 256, pitch_predictor_kernel_size: int = 5, pitch_predictor_dropout: float = 0.5,
                 pitch_embed_kernel_size: int = 1, pitch_embed_dropout: float = 0.0, stop_gradient_from_pitch_predictor: bool = True,  # pretrained spk emb
                 spk_embed_dim: int = None,  # training related
                 transformer_enc_dropout_rate: float = 0.2, transformer_enc_positional_dropout_rate: float = 0.2,
                 transformer_enc_attn_dropout_rate: float = 0.2, transformer_dec_dropout_rate: float = 0.2,
                 transformer_dec_positional_dropout_rate: float = 0.2, transformer_dec_attn_dropout_rate: float = 0.2,
                 duration_predictor_dropout_rate: float = 0.2, postnet_dropout_rate: float = 0.5, init_type: str = "xavier_uniform",
                 init_enc_alpha: float = 1.0, init_dec_alpha: float = 1.0, use_masking: bool = False, use_weighted_masking: bool = True):
        super().__init__()

        # store hyperparameters
        self.idim = idim
        self.odim = odim
        self.eos = 1
        self.reduction_factor = reduction_factor
        self.stop_gradient_from_pitch_predictor = stop_gradient_from_pitch_predictor
        self.stop_gradient_from_energy_predictor = stop_gradient_from_energy_predictor
        self.use_scaled_pos_enc = use_scaled_pos_enc
        self.spk_embed_dim = spk_embed_dim

        # use idx 0 as padding idx
        self.padding_idx = 0

        # define encoder
        encoder_input_layer = torch.nn.Embedding(num_embeddings=idim, embedding_dim=adim, padding_idx=self.padding_idx)
        self.encoder = Conformer(idim=idim, attention_dim=adim, attention_heads=aheads, linear_units=eunits, num_blocks=elayers,
                                 input_layer=encoder_input_layer, dropout_rate=transformer_enc_dropout_rate,
                                 positional_dropout_rate=transformer_enc_positional_dropout_rate, attention_dropout_rate=transformer_enc_attn_dropout_rate,
                                 normalize_before=encoder_normalize_before, concat_after=encoder_concat_after,
                                 positionwise_conv_kernel_size=positionwise_conv_kernel_size, macaron_style=use_macaron_style_in_conformer,
                                 use_cnn_module=use_cnn_in_conformer, cnn_module_kernel=conformer_enc_kernel_size, zero_triu=False)

        # define additional projection for speaker embedding
        if self.spk_embed_dim is not None:
            self.projection = torch.nn.Linear(adim + self.spk_embed_dim, adim)

        # define duration predictor
        self.duration_predictor = DurationPredictor(idim=adim, n_layers=duration_predictor_layers, n_chans=duration_predictor_chans,
                                                    kernel_size=duration_predictor_kernel_size, dropout_rate=duration_predictor_dropout_rate, )

        # define pitch predictor
        self.pitch_predictor = VariancePredictor(idim=adim, n_layers=pitch_predictor_layers, n_chans=pitch_predictor_chans,
                                                 kernel_size=pitch_predictor_kernel_size, dropout_rate=pitch_predictor_dropout)
        # continuous pitch + FastPitch style avg
        self.pitch_embed = torch.nn.Sequential(
            torch.nn.Conv1d(in_channels=1, out_channels=adim, kernel_size=pitch_embed_kernel_size, padding=(pitch_embed_kernel_size - 1) // 2),
            torch.nn.Dropout(pitch_embed_dropout))

        # define energy predictor
        self.energy_predictor = VariancePredictor(idim=adim, n_layers=energy_predictor_layers, n_chans=energy_predictor_chans,
                                                  kernel_size=energy_predictor_kernel_size, dropout_rate=energy_predictor_dropout)
        # continuous energy + FastPitch style avg
        self.energy_embed = torch.nn.Sequential(
            torch.nn.Conv1d(in_channels=1, out_channels=adim, kernel_size=energy_embed_kernel_size, padding=(energy_embed_kernel_size - 1) // 2),
            torch.nn.Dropout(energy_embed_dropout))

        # define length regulator
        self.length_regulator = LengthRegulator()

        self.decoder = Conformer(idim=0, attention_dim=adim, attention_heads=aheads, linear_units=dunits, num_blocks=dlayers, input_layer=None,
                                 dropout_rate=transformer_dec_dropout_rate, positional_dropout_rate=transformer_dec_positional_dropout_rate,
                                 attention_dropout_rate=transformer_dec_attn_dropout_rate, normalize_before=decoder_normalize_before,
                                 concat_after=decoder_concat_after, positionwise_conv_kernel_size=positionwise_conv_kernel_size,
                                 macaron_style=use_macaron_style_in_conformer, use_cnn_module=use_cnn_in_conformer, cnn_module_kernel=conformer_dec_kernel_size)

        # define final projection
        self.feat_out = torch.nn.Linear(adim, odim * reduction_factor)

        # define postnet
        self.postnet = PostNet(idim=idim, odim=odim, n_layers=postnet_layers, n_chans=postnet_chans, n_filts=postnet_filts, use_batch_norm=use_batch_norm,
                               dropout_rate=postnet_dropout_rate)

        # initialize parameters
        self._reset_parameters(init_type=init_type, init_enc_alpha=init_enc_alpha, init_dec_alpha=init_dec_alpha)

        # define criterions
        self.criterion = FastSpeech2Loss(use_masking=use_masking, use_weighted_masking=use_weighted_masking)

    def forward(self, text_tensors: torch.Tensor, text_lengths: torch.Tensor, gold_speech: torch.Tensor, speech_lengths: torch.Tensor,
                gold_durations: torch.Tensor, gold_pitch: torch.Tensor, gold_energy: torch.Tensor, spembs: torch.Tensor = None):
        """
        Calculate forward propagation.

        Args:
            text_tensors (LongTensor): Batch of padded text vectors (B, Tmax).
            text_lengths (LongTensor): Batch of lengths of each input (B,).
            gold_speech (Tensor): Batch of padded target features (B, Lmax, odim).
            speech_lengths (LongTensor): Batch of the lengths of each target (B,).
            gold_durations (LongTensor): Batch of padded durations (B, Tmax + 1).
            durations_lengths (LongTensor): Batch of duration lengths (B, Tmax + 1).
            gold_pitch (Tensor): Batch of padded token-averaged pitch (B, Tmax + 1, 1).
            pitch_lengths (LongTensor): Batch of pitch lengths (B, Tmax + 1).
            gold_energy (Tensor): Batch of padded token-averaged energy (B, Tmax + 1, 1).
            energy_lengths (LongTensor): Batch of energy lengths (B, Tmax + 1).
            spembs (Tensor, optional): Batch of speaker embeddings (B, spk_embed_dim).

        Returns:
            Tensor: Loss scalar value.
            Dict: Statistics to be monitored.
            Tensor: Weight value.
        """

        # Texts don't have the stop token in them because they are freshly made,
        # but all of the other stuff is based on the teacher model, which already
        # produces outputs for the stop token. So durations, pitch end energies all
        # have one more element than the text.

        # And now we add the missing EOS token also to the text.
        text_tensors_including_eos = F.pad(text_tensors, [0, 1], "constant", self.padding_idx)
        for i, l in enumerate(text_lengths):
            text_tensors_including_eos[i, l] = self.eos
        text_lengths_including_eos = text_lengths + 1

        # forward propagation
        before_outs, after_outs, d_outs, p_outs, e_outs = self._forward(text_tensors_including_eos, text_lengths_including_eos, gold_speech, speech_lengths,
                                                                        gold_durations, gold_pitch, gold_energy, spembs=spembs, is_inference=False)

        # modify mod part of groundtruth (speaking pace)
        if self.reduction_factor > 1:
            speech_lengths = speech_lengths.new([olen - olen % self.reduction_factor for olen in speech_lengths])

        # calculate loss
        l1_loss, duration_loss, pitch_loss, energy_loss = self.criterion(after_outs=after_outs, before_outs=before_outs, d_outs=d_outs, p_outs=p_outs,
                                                                         e_outs=e_outs, ys=gold_speech, ds=gold_durations, ps=gold_pitch, es=gold_energy,
                                                                         ilens=text_lengths_including_eos, olens=speech_lengths)
        loss = l1_loss + duration_loss + pitch_loss + energy_loss

        return loss

    def _forward(self, text_tensors: torch.Tensor, text_lens: torch.Tensor, gold_speech: torch.Tensor = None, speech_lens: torch.Tensor = None,
                 gold_durations: torch.Tensor = None, gold_pitch: torch.Tensor = None, gold_energy: torch.Tensor = None, spembs: torch.Tensor = None,
                 is_inference: bool = False, alpha: float = 1.0):
        # forward encoder
        text_masks = self._source_mask(text_lens)
        encoded_texts, _ = self.encoder(text_tensors, text_masks)  # (B, Tmax, adim)

        # integrate speaker embedding
        if self.spk_embed_dim is not None:
            encoded_texts = self._integrate_with_spk_embed(encoded_texts, spembs)

        # forward duration predictor and variance predictors
        d_masks = make_pad_mask(text_lens).to(text_tensors.device)

        if self.stop_gradient_from_pitch_predictor:
            pitch_predictions = self.pitch_predictor(encoded_texts.detach(), d_masks.unsqueeze(-1))
        else:
            pitch_predictions = self.pitch_predictor(encoded_texts, d_masks.unsqueeze(-1))
        if self.stop_gradient_from_energy_predictor:
            energy_predictions = self.energy_predictor(encoded_texts.detach(), d_masks.unsqueeze(-1))
        else:
            energy_predictions = self.energy_predictor(encoded_texts, d_masks.unsqueeze(-1))

        if is_inference:
            d_outs = self.duration_predictor.inference(encoded_texts, d_masks)  # (B, Tmax)
            # use prediction in inference
            p_embs = self.pitch_embed(pitch_predictions.transpose(1, 2)).transpose(1, 2)
            e_embs = self.energy_embed(energy_predictions.transpose(1, 2)).transpose(1, 2)
            encoded_texts = encoded_texts + e_embs + p_embs
            encoded_texts = self.length_regulator(encoded_texts, d_outs, alpha)  # (B, Lmax, adim)
        else:
            d_outs = self.duration_predictor(encoded_texts, d_masks)
            # use groundtruth in training
            p_embs = self.pitch_embed(gold_pitch.transpose(1, 2)).transpose(1, 2)
            e_embs = self.energy_embed(gold_energy.transpose(1, 2)).transpose(1, 2)
            encoded_texts = encoded_texts + e_embs + p_embs
            encoded_texts = self.length_regulator(encoded_texts, gold_durations)  # (B, Lmax, adim)

        # forward decoder
        if speech_lens is not None and not is_inference:
            if self.reduction_factor > 1:
                olens_in = speech_lens.new([olen // self.reduction_factor for olen in speech_lens])
            else:
                olens_in = speech_lens
            h_masks = self._source_mask(olens_in)
        else:
            h_masks = None
        zs, _ = self.decoder(encoded_texts, h_masks)  # (B, Lmax, adim)
        before_outs = self.feat_out(zs).view(zs.size(0), -1, self.odim)  # (B, Lmax, odim)

        # postnet -> (B, Lmax//r * r, odim)
        after_outs = before_outs + self.postnet(before_outs.transpose(1, 2)).transpose(1, 2)

        return before_outs, after_outs, d_outs, pitch_predictions, energy_predictions

    def inference(self, text: torch.Tensor, speech: torch.Tensor = None, spembs: torch.Tensor = None, durations: torch.Tensor = None,
                  pitch: torch.Tensor = None, energy: torch.Tensor = None, alpha: float = 1.0, use_teacher_forcing: bool = False):
        """
        Generate the sequence of features given the sequences of characters.

        Args:
            text (LongTensor): Input sequence of characters (T,).
            speech (Tensor, optional): Feature sequence to extract style (N, idim).
            spembs (Tensor, optional): Speaker embedding vector (spk_embed_dim,).
            durations (LongTensor, optional): Groundtruth of duration (T + 1,).
            pitch (Tensor, optional): Groundtruth of token-averaged pitch (T + 1, 1).
            energy (Tensor, optional): Groundtruth of token-averaged energy (T + 1, 1).
            alpha (float, optional): Alpha to control the speed.
            use_teacher_forcing (bool, optional): Whether to use teacher forcing.
                If true, groundtruth of duration, pitch and energy will be used.

        Returns:
            Tensor: Output sequence of features (L, odim).

        """
        self.eval()
        x, y = text, speech
        spemb, d, p, e = spembs, durations, pitch, energy

        # setup batch axis
        ilens = torch.tensor([x.shape[0]], dtype=torch.long, device=x.device)
        xs, ys = x.unsqueeze(0), None
        if y is not None:
            ys = y.unsqueeze(0)
        if spemb is not None:
            spembs = spemb.unsqueeze(0)

        if use_teacher_forcing:
            # use groundtruth of duration, pitch, and energy
            ds, ps, es = d.unsqueeze(0), p.unsqueeze(0), e.unsqueeze(0)
            _, outs, *_ = self._forward(xs, ilens, ys, gold_durations=ds, gold_pitch=ps, gold_energy=es, spembs=spembs)  # (1, L, odim)
        else:
            _, outs, *_ = self._forward(xs, ilens, ys, spembs=spembs, is_inference=True, alpha=alpha)  # (1, L, odim)
        self.train()
        return outs[0]

    def _integrate_with_spk_embed(self, hs: torch.Tensor, spembs: torch.Tensor) -> torch.Tensor:
        """
        Integrate speaker embedding with hidden states.

        Args:
            hs (Tensor): Batch of hidden state sequences (B, Tmax, adim).
            spembs (Tensor): Batch of speaker embeddings (B, spk_embed_dim).

        Returns:
            Tensor: Batch of integrated hidden state sequences (B, Tmax, adim).

        """
        # concat hidden states with spk embeds and then apply projection
        spembs = F.normalize(spembs).unsqueeze(1).expand(-1, hs.size(1), -1)
        hs = self.projection(torch.cat([hs, spembs], dim=-1))
        return hs

    def _source_mask(self, ilens: torch.Tensor) -> torch.Tensor:
        """
        Make masks for self-attention.

        Args:
            ilens (LongTensor): Batch of lengths (B,).

        Returns:
            Tensor: Mask tensor for self-attention.
                dtype=torch.uint8 in PyTorch 1.2-
                dtype=torch.bool in PyTorch 1.2+ (including 1.2)

        """
        x_masks = make_non_pad_mask(ilens).to(ilens.device)
        return x_masks.unsqueeze(-2)

    def _reset_parameters(self, init_type: str, init_enc_alpha: float, init_dec_alpha: float):
        # initialize parameters
        if init_type != "pytorch":
            initialize(self, init_type)


def build_reference_fastspeech2_model(model_name):
    model = FastSpeech2(idim=133, odim=80, spk_embed_dim=None).to("cpu")
    params = torch.load(os.path.join("Models", model_name), map_location='cpu')["model"]
    model.load_state_dict(params)
    return model
