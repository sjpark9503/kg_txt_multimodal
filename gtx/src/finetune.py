# Base packages
import logging
import math
import os
from dataclasses import dataclass, field
from glob import glob
from typing import Optional
from torch.utils.data import ConcatDataset
import torch

# Own implementation
from utils.parameters import parser
from utils.dataset import get_dataset
from utils.data_collator import NegativeSampling_DataCollator, AdmLvlPred_DataCollator, ErrorDetection_DataCollator
from model import GTXForRanking, GTXForKGTokPredAndMaskedLM, GTXForAdmLvlPrediction, GTXForErrorDetection
from trainer import Trainer

# From Huggingface transformers package
from transformers import (
    CONFIG_MAPPING,
    MODEL_WITH_LM_HEAD_MAPPING,
    LxmertConfig,
    LxmertTokenizer,
    PreTrainedTokenizer,
    # Trainer,
    set_seed,
)

logger = logging.getLogger(__name__)

MODEL_CONFIG_CLASSES = list(MODEL_WITH_LM_HEAD_MAPPING.keys())
MODEL_TYPES = tuple(conf.model_type for conf in MODEL_CONFIG_CLASSES)

def main():
    # See all possible arguments in src/transformers/training_args.py
    # or by passing the --help flag to this script.
    # We now keep distinct sets of args, for a cleaner separation of concerns.
    model_args, data_args, training_args = parser.parse_args_into_dataclasses()

    if data_args.eval_data_file is None and training_args.do_eval:
        raise ValueError(
            "Cannot do evaluation without an evaluation data file. Either supply a file to --eval_data_file "
            "or remove the --do_eval argument."
        )
    if (
        os.path.exists(training_args.output_dir)
        and os.listdir(training_args.output_dir)
        and training_args.do_train
        and not training_args.overwrite_output_dir
    ):
        raise ValueError(
            f"Output directory ({training_args.output_dir}) already exists and is not empty. Use --overwrite_output_dir to overcome."
        )

    # Setup logging
    logging.basicConfig(
        format="%(asctime)s - %(message)s",
        datefmt="%m/%d %H:%M",
        level=logging.INFO if training_args.local_rank in [-1, 0] else logging.WARN,
    )
    logger.warning(
        "Process rank: %s, device: %s, n_gpu: %s, distributed training: %s, 16-bits training: %s",
        training_args.local_rank,
        training_args.device,
        training_args.n_gpu,
        bool(training_args.local_rank != -1),
        training_args.fp16,
    )
    logger.info("Training/evaluation parameters %s", training_args)

    # Set seed
    set_seed(training_args.seed)

    # Load pretrained model and tokenizer
    #
    # Distributed training:
    # The .from_pretrained methods guarantee that only one local process can concurrently
    # download model & vocab.

    if model_args.config_name:
        config = LxmertConfig.from_pretrained(model_args.config_name, cache_dir=model_args.cache_dir)
    elif model_args.model_name_or_path:
        config = LxmertConfig.from_pretrained(model_args.model_name_or_path, cache_dir=model_args.cache_dir)
    else:
        config = CONFIG_MAPPING[model_args.model_type]()
        logger.warning("You are instantiating a new config instance from scratch.")
    config.use_ce_pooler=True
    if model_args.tokenizer_name:
        tokenizer = LxmertTokenizer.from_pretrained(model_args.tokenizer_name, cache_dir=model_args.cache_dir)
    elif model_args.model_name_or_path:
        tokenizer = LxmertTokenizer.from_pretrained(model_args.model_name_or_path, cache_dir=model_args.cache_dir)
    else:
        raise ValueError(
            "You are instantiating a new tokenizer from scratch. This is not supported, but you can do it from another script, save it,"
            "and load it from here, using --tokenizer_name"
        )
    if ((config.num_attention_heads % config.num_relations) != 0) and config.gcn and ('Multi' in training_args.output_dir):
        raise ValueError(
            "# attentions heads must be divisible by # relations"
        )

    if model_args.model_name_or_path:
        if 'retrieval' in training_args.task:
            # try:
            model = GTXForRanking.from_pretrained(
                model_args.model_name_or_path,
                from_tf=bool(".ckpt" in model_args.model_name_or_path),
                config=config,
                cache_dir=model_args.cache_dir,
            )
            # except:
            #     ckpt_path = os.path.join(model_args.model_name_or_path, 'pytorch_model.bin')
            #     load_model_dict = torch.load(ckpt_path)
            #     modified_model_dict = load_model_dict.copy()
            #     for param in load_model_dict:
            #         if 'pooler' in param:
            #             modified_model_dict.pop(param)
            #     torch.save(modified_model_dict, ckpt_path)

            #     model = GTXForRanking.from_pretrained(
            #         model_args.model_name_or_path,
            #         from_tf=bool(".ckpt" in model_args.model_name_or_path),
            #         config=config,
            #         cache_dir=model_args.cache_dir,
            #     )
        elif 'generation' in training_args.task: 
            model = GTXForKGTokPredAndMaskedLM.from_pretrained(
                model_args.model_name_or_path,
                from_tf=bool(".ckpt" in model_args.model_name_or_path),
                config=config,
                cache_dir=model_args.cache_dir,
            )
        elif 'adm' in training_args.task:
            try:
                config.use_ce_pooler=False
                model = GTXForAdmLvlPrediction.from_pretrained(
                    model_args.model_name_or_path,
                    from_tf=bool(".ckpt" in model_args.model_name_or_path),
                    config=config,
                    cache_dir=model_args.cache_dir,
                )
            except:
                ckpt_path = os.path.join(model_args.model_name_or_path, 'pytorch_model.bin')
                load_model_dict = torch.load(ckpt_path)
                modified_model_dict = load_model_dict.copy()
                for param in load_model_dict:
                    if 'multi_pooler' in param:
                        modified_model_dict.pop(param)
                torch.save(modified_model_dict, ckpt_path)
                model = GTXForAdmLvlPrediction.from_pretrained(
                    model_args.model_name_or_path,
                    from_tf=bool(".ckpt" in model_args.model_name_or_path),
                    config=config,
                    cache_dir=model_args.cache_dir,
                )
            db =  training_args.run_name.split('/')[3].split('_')[-1]
            class_weight = torch.tensor(torch.load(os.path.join(os.getcwd(),f'data/{db}/adm_class_weight')),requires_grad=False)
            model.class_weight = class_weight.to(training_args.device)
        elif 'detection' in training_args.task:
            model = GTXForErrorDetection.from_pretrained(
                model_args.model_name_or_path,
                from_tf=bool(".ckpt" in model_args.model_name_or_path),
                config=config,
                cache_dir=model_args.cache_dir,
            )
        else:
            raise NotImplementedError("Not implemented task: %s", training_args.task)
    else:
        logger.info("Training new model from scratch")
        if 'retrieval' in training_args.task:
            # try:
            model = GTXForRanking(config)
            # except:
            #     ckpt_path = os.path.join(model_args.model_name_or_path, 'pytorch_model.bin')
            #     load_model_dict = torch.load(ckpt_path)
            #     modified_model_dict = load_model_dict.copy()
            #     for param in load_model_dict:
            #         if 'pooler' in param:
            #             modified_model_dict.pop(param)
            #     torch.save(modified_model_dict, ckpt_path)

            #     model = GTXForRanking.from_pretrained(
            #         model_args.model_name_or_path,
            #         from_tf=bool(".ckpt" in model_args.model_name_or_path),
            #         config=config,
            #         cache_dir=model_args.cache_dir,
            #     )
        elif 'generation' in training_args.task: 
            model = GTXForKGTokPredAndMaskedLM(config)
        elif 'adm' in training_args.task:
            config.use_ce_pooler=False
            model = GTXForAdmLvlPrediction(config)
            #db =  training_args.run_name.split('/')[0].split('_')[-1]
            #class_weight_dict = torch.load(os.path.join(os.getcwd(),f'data/{db}/adm_class_weight'))
            #model.class_weight = torch.tensor(list(dict(sorted(class_weight_dict.items())).values()),requires_grad=False).to(training_args.device)
        elif 'detection' in training_args.task:
            model = GTXForErrorDetection(config)
        else:
            raise NotImplementedError("Not implemented task: %s", training_args.task)
    logger.info(config)
    #model.resize_token_embeddings(len(tokenizer))

    if config.model_type in ["bert", "roberta", "distilbert", "camembert"] and not data_args.mlm:
        raise ValueError(
            "BERT and RoBERTa-like models do not have LM heads but masked LM heads. They must be run using the"
            "--mlm flag (masked language modeling)."
        )

    if data_args.block_size <= 0:
        data_args.block_size = tokenizer.max_len
        # Our input block size will be the max possible for the model
    else:
        data_args.block_size = min(data_args.block_size, tokenizer.max_len)

    # Get datasets
    train_dataset = get_dataset(data_args,
                                tokenizer=tokenizer,
                                token_type_vocab=config.token_type_vocab,
                                )
    logger.info(train_dataset[0])
    eval_dataset = get_dataset(data_args,
                                tokenizer=tokenizer,
                                token_type_vocab=config.token_type_vocab,
                                evaluate=True,
                                )
    test_dataset = get_dataset(data_args,
                                tokenizer=tokenizer,
                                token_type_vocab=config.token_type_vocab,
                                test=True
                                ) if training_args.do_eval else None
    eval_data_collator = None
    if 'retrieval' in training_args.task:
        data_collator = NegativeSampling_DataCollator(tokenizer=tokenizer,
                                                      kg_special_token_ids=config.kg_special_token_ids,
                                                      n_negatives=training_args.n_negatives)
        eval_data_collator = NegativeSampling_DataCollator(tokenizer=tokenizer,
                                                      kg_special_token_ids=config.kg_special_token_ids)
    elif 'adm' in training_args.task:
        data_collator = AdmLvlPred_DataCollator(tokenizer=tokenizer,
                                                num_kg_labels=config.num_kg_labels,
                                                kg_special_token_ids=config.kg_special_token_ids)
    elif 'detection' in training_args.task:
        data_collator = ErrorDetection_DataCollator(tokenizer=tokenizer,
                                                kg_special_token_ids=config.kg_special_token_ids,
                                                #num_kg_labels=config.num_kg_labels,
                                                kg_size=config.vocab_size['kg'],
                                                corruption_probability=0.2 if 'text' in training_args.task else 0.1,
                                                task = training_args.task)
    elif 'generation' in training_args.task: 
        from utils.data_collator import UniLM_DataCollator
        data_collator = UniLM_DataCollator(tokenizer=tokenizer,
                                           kg_special_token_ids=config.kg_special_token_ids)
    else:
        raise NotImplementedError("Not implemented task")
    # Initialize our Trainer
    trainer = Trainer(
        model=model,
        args=training_args,
        data_collator=data_collator,
        eval_data_collator=eval_data_collator,
        train_dataset=train_dataset,
        eval_dataset=eval_dataset,
        test_dataset=test_dataset
    )

    # Training
    if training_args.do_train:
        model_path = (
            model_args.model_name_or_path
            if model_args.model_name_or_path is not None and os.path.isdir(model_args.model_name_or_path)
            else None
        )
        trainer.train(model_path=model_path)
        # trainer.save_model()
        # For convenience, we also re-save the tokenizer to the same directory,
        # so that you can share your model easily on huggingface.co/models =)
        if trainer.is_world_master():
            tokenizer.save_pretrained(training_args.output_dir)

    if 'detection' in training_args.task:
        model = GTXForErrorDetection.from_pretrained(
            training_args.output_dir,
            from_tf=bool(".ckpt" in training_args.output_dir),
            config=config,
            cache_dir=model_args.cache_dir,
        )
        trainer.model = model.to(training_args.device)
        trainer.args.top_k = 1
        outputs = trainer.predict(test_dataset)
        trainer.args.top_k = 3
        outputs = trainer.predict(test_dataset)
        trainer.args.top_k = 5
        outputs = trainer.predict(test_dataset)
        trainer.args.top_k = 10
        outputs = trainer.predict(test_dataset)

    elif 'adm' in training_args.task:
        model = GTXForAdmLvlPrediction.from_pretrained(
            training_args.output_dir,
            from_tf=bool(".ckpt" in training_args.output_dir),
            config=config,
            cache_dir=model_args.cache_dir,
        )
        trainer.model = model.to(training_args.device)
        trainer.args.top_k = 1
        outputs = trainer.predict(test_dataset)
        trainer.args.top_k = 3
        outputs = trainer.predict(test_dataset)
        trainer.args.top_k = 5
        outputs = trainer.predict(test_dataset)
        trainer.args.top_k = 10
        outputs = trainer.predict(test_dataset)

def _mp_fn(index):
    # For xla_spawn (TPUs)
    main()


if __name__ == "__main__":
    main()